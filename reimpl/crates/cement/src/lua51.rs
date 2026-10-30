//! Lua 5.1 bytecode undumper.
//!
//! Prototype 2 compiles its scripts to Lua 5.1 chunks (`\x1bLua\x51`) and ships
//! them as `scripts/{init,gameinit,black_net,missiondefinit,datainit}.lua`
//! inside `boot.rcf`. Two deviations from stock `luac` matter:
//!
//! * `lua_Number` is **float32** (the header's number size is 4), so numeric
//!   constants must be read as `f32`;
//! * the source name of every chunk is empty, so recovered function identity
//!   has to come from line ranges and constant tables rather than names.
//!
//! Shipped totals: 1,574 functions, ~72,000 instructions, ~22,600 string
//! constants across the five scripts.

use std::fmt;

/// Bytecode parse failures.
#[derive(Debug)]
pub enum LuaError {
    /// Missing `\x1bLua` signature.
    BadSignature,
    /// Not Lua 5.1.
    UnsupportedVersion(u8),
    /// Not the official binary chunk format.
    UnsupportedFormat(u8),
    /// Only little-endian chunks occur in shipped data.
    UnsupportedEndian(u8),
    /// Type sizes other than {int 4, size_t 4, instr 4, number 4|8}.
    UnsupportedSizes,
    /// Ran out of bytes.
    Truncated,
    /// Unknown constant tag.
    BadConstant(u8),
}

impl fmt::Display for LuaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LuaError::BadSignature => write!(f, "missing Lua signature"),
            LuaError::UnsupportedVersion(v) => write!(f, "unsupported Lua version 0x{v:02x}"),
            LuaError::UnsupportedFormat(f_) => write!(f, "unsupported chunk format {f_}"),
            LuaError::UnsupportedEndian(e) => write!(f, "unsupported endian flag {e}"),
            LuaError::UnsupportedSizes => write!(f, "unsupported integer/number sizes"),
            LuaError::Truncated => write!(f, "truncated bytecode"),
            LuaError::BadConstant(t) => write!(f, "bad constant tag {t}"),
        }
    }
}

impl std::error::Error for LuaError {}

/// A constant from a function's constant table.
#[derive(Clone, Debug, PartialEq)]
pub enum Constant {
    /// `nil`.
    Nil,
    /// Boolean.
    Bool(bool),
    /// Numeric constant; the engine uses 32-bit floats.
    Number(f64),
    /// String constant (Lua strings are byte strings).
    Str(Vec<u8>),
}

impl Constant {
    /// Interpret the constant as UTF-8 text when it is a string.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Constant::Str(s) => std::str::from_utf8(s).ok(),
            _ => None,
        }
    }
}

/// One function prototype.
#[derive(Clone, Debug, Default)]
pub struct Proto {
    /// Source name recorded in the chunk (empty in shipped Prototype 2 data).
    pub source: Option<Vec<u8>>,
    /// First line of the function in the original source.
    pub line_defined: i32,
    /// Last line of the function in the original source.
    pub last_line: i32,
    /// Number of upvalues.
    pub nups: u8,
    /// Number of fixed parameters.
    pub num_params: u8,
    /// `1` when the function is vararg.
    pub is_vararg: u8,
    /// Maximum stack slots.
    pub max_stack: u8,
    /// Raw instruction words.
    pub code: Vec<u32>,
    /// Constants.
    pub constants: Vec<Constant>,
    /// Nested functions.
    pub protos: Vec<Proto>,
    /// Line numbers per instruction.
    pub line_info: Vec<i32>,
    /// `(name, start_pc, end_pc)` for locals.
    pub locals: Vec<(Option<Vec<u8>>, i32, i32)>,
    /// Names of upvalues.
    pub upvalue_names: Vec<Option<Vec<u8>>>,
}

impl Proto {
    /// Depth-first iteration over this function and all nested functions.
    pub fn walk(&self) -> impl Iterator<Item = &Proto> {
        let mut stack = vec![self];
        std::iter::from_fn(move || {
            let next = stack.pop()?;
            for c in next.protos.iter().rev() {
                stack.push(c);
            }
            Some(next)
        })
    }

    /// All string constants in this function.
    pub fn strings(&self) -> impl Iterator<Item = &[u8]> {
        self.constants.iter().filter_map(|c| match c {
            Constant::Str(s) => Some(s.as_slice()),
            _ => None,
        })
    }
}

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
    num_size: usize,
    num_integral: bool,
}

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], LuaError> {
        let end = self.pos.checked_add(n).ok_or(LuaError::Truncated)?;
        let s = self.data.get(self.pos..end).ok_or(LuaError::Truncated)?;
        self.pos = end;
        Ok(s)
    }
    fn u8(&mut self) -> Result<u8, LuaError> {
        Ok(self.take(1)?[0])
    }
    fn u32(&mut self) -> Result<u32, LuaError> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn i32(&mut self) -> Result<i32, LuaError> {
        Ok(i32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn number(&mut self) -> Result<f64, LuaError> {
        let size = self.num_size;
        let raw = self.take(size)?;
        Ok(if self.num_integral {
            if size == 4 {
                i32::from_le_bytes(raw.try_into().unwrap()) as f64
            } else {
                i64::from_le_bytes(raw.try_into().unwrap()) as f64
            }
        } else if size == 4 {
            f32::from_le_bytes(raw.try_into().unwrap()) as f64
        } else {
            f64::from_le_bytes(raw.try_into().unwrap())
        })
    }
    fn string(&mut self) -> Result<Option<Vec<u8>>, LuaError> {
        let n = self.u32()? as usize;
        if n == 0 {
            return Ok(None);
        }
        let s = self.take(n)?;
        Ok(Some(s[..n - 1].to_vec()))
    }
}

/// Parse a Lua 5.1 binary chunk, returning its main function.
pub fn load(data: &[u8]) -> Result<Proto, LuaError> {
    let mut r = Reader { data, pos: 0, num_size: 8, num_integral: false };
    if r.take(4)? != b"\x1bLua" {
        return Err(LuaError::BadSignature);
    }
    let version = r.u8()?;
    if version != 0x51 {
        return Err(LuaError::UnsupportedVersion(version));
    }
    let format = r.u8()?;
    if format != 0 {
        return Err(LuaError::UnsupportedFormat(format));
    }
    let endian = r.u8()?;
    if endian != 1 {
        return Err(LuaError::UnsupportedEndian(endian));
    }
    let int_size = r.u8()?;
    let size_t = r.u8()?;
    let instr = r.u8()?;
    let num_size = r.u8()?;
    let integral = r.u8()?;
    if (int_size, size_t, instr) != (4, 4, 4) || !(num_size == 4 || num_size == 8) {
        return Err(LuaError::UnsupportedSizes);
    }
    r.num_size = num_size as usize;
    r.num_integral = integral != 0;
    load_function(&mut r)
}

fn load_function(r: &mut Reader<'_>) -> Result<Proto, LuaError> {
    let mut f = Proto {
        source: r.string()?,
        line_defined: r.i32()?,
        last_line: r.i32()?,
        nups: r.u8()?,
        num_params: r.u8()?,
        is_vararg: r.u8()?,
        max_stack: r.u8()?,
        ..Default::default()
    };
    let n = r.u32()? as usize;
    f.code.reserve(n);
    for _ in 0..n {
        f.code.push(r.u32()?);
    }
    let n = r.u32()? as usize;
    f.constants.reserve(n);
    for _ in 0..n {
        let tag = r.u8()?;
        f.constants.push(match tag {
            0 => Constant::Nil,
            1 => Constant::Bool(r.u8()? != 0),
            3 => Constant::Number(r.number()?),
            4 => Constant::Str(r.string()?.unwrap_or_default()),
            other => return Err(LuaError::BadConstant(other)),
        });
    }
    let n = r.u32()? as usize;
    f.protos.reserve(n);
    for _ in 0..n {
        f.protos.push(load_function(r)?);
    }
    let n = r.u32()? as usize;
    f.line_info.reserve(n);
    for _ in 0..n {
        f.line_info.push(r.i32()?);
    }
    let n = r.u32()? as usize;
    f.locals.reserve(n);
    for _ in 0..n {
        let name = r.string()?;
        let start = r.i32()?;
        let end = r.i32()?;
        f.locals.push((name, start, end));
    }
    let n = r.u32()? as usize;
    f.upvalue_names.reserve(n);
    for _ in 0..n {
        f.upvalue_names.push(r.string()?);
    }
    Ok(f)
}

/// Lua 5.1 opcode names, indexed by the 6-bit opcode field.
pub const OPCODES: [&str; 38] = [
    "MOVE", "LOADK", "LOADBOOL", "LOADNIL", "GETUPVAL", "GETGLOBAL", "GETTABLE", "SETGLOBAL",
    "SETUPVAL", "SETTABLE", "NEWTABLE", "SELF", "ADD", "SUB", "MUL", "DIV", "MOD", "POW", "UNM",
    "NOT", "LEN", "CONCAT", "JMP", "EQ", "LT", "LE", "TEST", "TESTSET", "CALL", "TAILCALL",
    "RETURN", "FORLOOP", "FORPREP", "TFORLOOP", "SETLIST", "CLOSE", "CLOSURE", "VARARG",
];

/// A decoded instruction.
#[derive(Clone, Copy, Debug)]
pub struct Instruction {
    /// Opcode index into [`OPCODES`].
    pub op: u8,
    /// First operand (8 bits).
    pub a: u8,
    /// Second operand (9 bits).
    pub b: u16,
    /// Third operand (9 bits).
    pub c: u16,
    /// Extended operand (18 bits).
    pub bx: u32,
    /// Signed extended operand.
    pub sbx: i32,
}

/// Decode one 32-bit instruction word.
pub fn decode(word: u32) -> Instruction {
    let op = (word & 0x3F) as u8;
    let a = ((word >> 6) & 0xFF) as u8;
    let c = ((word >> 14) & 0x1FF) as u16;
    let b = ((word >> 23) & 0x1FF) as u16;
    let bx = (word >> 14) & 0x3_FFFF;
    Instruction { op, a, b, c, bx, sbx: bx as i32 - 131_071 }
}

/// Summary counts used by tooling and tests.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct ChunkStats {
    /// Number of function prototypes.
    pub protos: usize,
    /// Total instructions.
    pub instructions: usize,
    /// Total string constants (with duplicates).
    pub string_constants: usize,
}
