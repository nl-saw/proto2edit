//! The `RZ` wrapper used for `.p3d.rz` payloads.
//!
//! Layout observed on every shipped `.rz` payload:
//!
//! ```text
//! 0x00  char[2]  "RZ"
//! 0x02  u8[6]    reserved (zero)
//! 0x08  u64      declared uncompressed length
//! 0x10  ...      one raw zlib stream to the end of the entry
//! ```
//!
//! Decompression needs an inflate implementation. To stay dependency-free the
//! crate ships a compact inflate that covers the two block types the shipped
//! payloads use (stored and dynamic Huffman), which is enough for every
//! archive entry in Prototype 2.

use std::fmt;

/// Why a payload could not be decoded.
#[derive(Debug)]
pub enum RzError {
    /// The payload does not start with the `RZ` marker.
    NotRz,
    /// The zlib stream is malformed or truncated.
    Inflate(&'static str),
    /// The decoded length disagrees with the declared length.
    Length { declared: u64, actual: usize },
}

impl fmt::Display for RzError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RzError::NotRz => write!(f, "payload is not an RZ container"),
            RzError::Inflate(m) => write!(f, "invalid zlib stream: {m}"),
            RzError::Length { declared, actual } => {
                write!(f, "RZ declared {declared} bytes but decoded {actual}")
            }
        }
    }
}

impl std::error::Error for RzError {}

/// True when `data` begins with the RZ marker.
pub fn is_rz(data: &[u8]) -> bool {
    data.len() >= 16 && &data[..2] == b"RZ"
}

/// Unwrap an RZ payload, returning the decompressed bytes.
///
/// Non-RZ inputs are returned unchanged, mirroring the engine, which tries the
/// compressed path first and falls back to the raw payload.
pub fn unwrap_rz(data: &[u8]) -> Result<Vec<u8>, RzError> {
    if !is_rz(data) {
        return Ok(data.to_vec());
    }
    let declared = u64::from_le_bytes(data[8..16].try_into().unwrap());
    let out = zlib_inflate(&data[16..]).map_err(RzError::Inflate)?;
    if declared != out.len() as u64 {
        return Err(RzError::Length { declared, actual: out.len() });
    }
    Ok(out)
}

/// Minimal zlib (RFC 1950/1951) decompressor.
pub fn zlib_inflate(src: &[u8]) -> Result<Vec<u8>, &'static str> {
    if src.len() < 2 {
        return Err("stream shorter than the zlib header");
    }
    let (cmf, flg) = (src[0], src[1]);
    if cmf & 0x0f != 8 {
        return Err("zlib compression method is not deflate");
    }
    if (u16::from(cmf) * 256 + u16::from(flg)) % 31 != 0 {
        return Err("bad zlib header check");
    }
    if flg & 0x20 != 0 {
        return Err("preset dictionaries are not supported");
    }
    inflate_raw(&src[2..])
}

struct BitReader<'a> {
    data: &'a [u8],
    pos: usize,
    bitbuf: u32,
    bitcnt: u32,
}

impl<'a> BitReader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, pos: 0, bitbuf: 0, bitcnt: 0 }
    }

    fn bits(&mut self, need: u32) -> Result<u32, &'static str> {
        while self.bitcnt < need {
            if self.pos >= self.data.len() {
                return Err("unexpected end of deflate stream");
            }
            self.bitbuf |= u32::from(self.data[self.pos]) << self.bitcnt;
            self.pos += 1;
            self.bitcnt += 8;
        }
        let v = self.bitbuf & ((1u32 << need) - 1);
        self.bitbuf >>= need;
        self.bitcnt -= need;
        Ok(v)
    }

    fn align(&mut self) {
        self.bitbuf = 0;
        self.bitcnt = 0;
    }
}

struct Huffman {
    counts: [u16; 16],
    symbols: Vec<u16>,
}

impl Huffman {
    fn new(lengths: &[u8]) -> Self {
        let mut counts = [0u16; 16];
        for &l in lengths {
            counts[l as usize] += 1;
        }
        counts[0] = 0;
        let mut offsets = [0u16; 16];
        for i in 1..16 {
            offsets[i] = offsets[i - 1] + counts[i - 1];
        }
        let mut symbols = vec![0u16; lengths.len()];
        for (sym, &l) in lengths.iter().enumerate() {
            if l != 0 {
                symbols[offsets[l as usize] as usize] = sym as u16;
                offsets[l as usize] += 1;
            }
        }
        Self { counts, symbols }
    }

    fn decode(&self, br: &mut BitReader<'_>) -> Result<u16, &'static str> {
        let mut code: i32 = 0;
        let mut first: i32 = 0;
        let mut index: i32 = 0;
        for len in 1..16 {
            code |= br.bits(1)? as i32;
            let count = self.counts[len] as i32;
            if code - first < count {
                return Ok(self.symbols[(index + (code - first)) as usize]);
            }
            index += count;
            first += count;
            first <<= 1;
            code <<= 1;
        }
        Err("invalid Huffman code")
    }
}

const LENGTH_BASE: [u16; 29] = [
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115,
    131, 163, 195, 227, 258,
];
const LENGTH_EXTRA: [u8; 29] = [
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0,
];
const DIST_BASE: [u16; 30] = [
    1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537,
    2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577,
];
const DIST_EXTRA: [u8; 30] = [
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12,
    13, 13,
];

fn inflate_raw(src: &[u8]) -> Result<Vec<u8>, &'static str> {
    let mut br = BitReader::new(src);
    let mut out: Vec<u8> = Vec::new();
    loop {
        let last = br.bits(1)?;
        let btype = br.bits(2)?;
        match btype {
            0 => {
                br.align();
                if br.pos + 4 > br.data.len() {
                    return Err("truncated stored block");
                }
                let len = u16::from_le_bytes([br.data[br.pos], br.data[br.pos + 1]]) as usize;
                let nlen = u16::from_le_bytes([br.data[br.pos + 2], br.data[br.pos + 3]]) as usize;
                if len != (!nlen & 0xffff) {
                    return Err("stored block length check failed");
                }
                br.pos += 4;
                if br.pos + len > br.data.len() {
                    return Err("truncated stored block payload");
                }
                out.extend_from_slice(&br.data[br.pos..br.pos + len]);
                br.pos += len;
            }
            1 => {
                let mut lit_lengths = [0u8; 288];
                for (i, v) in lit_lengths.iter_mut().enumerate() {
                    *v = match i {
                        0..=143 => 8,
                        144..=255 => 9,
                        256..=279 => 7,
                        _ => 8,
                    };
                }
                let lit = Huffman::new(&lit_lengths);
                let dist = Huffman::new(&[5u8; 30]);
                inflate_block(&mut br, &mut out, &lit, &dist)?;
            }
            2 => {
                let hlit = br.bits(5)? as usize + 257;
                let hdist = br.bits(5)? as usize + 1;
                let hclen = br.bits(4)? as usize + 4;
                const ORDER: [usize; 19] = [
                    16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15,
                ];
                let mut code_lengths = [0u8; 19];
                for &idx in ORDER.iter().take(hclen) {
                    code_lengths[idx] = br.bits(3)? as u8;
                }
                let code_huff = Huffman::new(&code_lengths);
                let mut lengths = vec![0u8; hlit + hdist];
                let mut i = 0;
                while i < lengths.len() {
                    let sym = code_huff.decode(&mut br)?;
                    match sym {
                        0..=15 => {
                            lengths[i] = sym as u8;
                            i += 1;
                        }
                        16 => {
                            if i == 0 {
                                return Err("repeat code with no previous length");
                            }
                            let prev = lengths[i - 1];
                            let n = 3 + br.bits(2)? as usize;
                            for _ in 0..n {
                                if i >= lengths.len() {
                                    return Err("length repeat overruns table");
                                }
                                lengths[i] = prev;
                                i += 1;
                            }
                        }
                        17 => {
                            let n = 3 + br.bits(3)? as usize;
                            i = (i + n).min(lengths.len());
                        }
                        18 => {
                            let n = 11 + br.bits(7)? as usize;
                            i = (i + n).min(lengths.len());
                        }
                        _ => return Err("invalid code length symbol"),
                    }
                }
                let lit = Huffman::new(&lengths[..hlit]);
                let dist = Huffman::new(&lengths[hlit..]);
                inflate_block(&mut br, &mut out, &lit, &dist)?;
            }
            _ => return Err("reserved deflate block type"),
        }
        if last == 1 {
            break;
        }
    }
    Ok(out)
}

fn inflate_block(
    br: &mut BitReader<'_>,
    out: &mut Vec<u8>,
    lit: &Huffman,
    dist: &Huffman,
) -> Result<(), &'static str> {
    loop {
        let sym = lit.decode(br)? as usize;
        if sym < 256 {
            out.push(sym as u8);
        } else if sym == 256 {
            return Ok(());
        } else {
            let li = sym - 257;
            if li >= LENGTH_BASE.len() {
                return Err("invalid length symbol");
            }
            let len = LENGTH_BASE[li] as usize + br.bits(LENGTH_EXTRA[li] as u32)? as usize;
            let dsym = dist.decode(br)? as usize;
            if dsym >= DIST_BASE.len() {
                return Err("invalid distance symbol");
            }
            let distance = DIST_BASE[dsym] as usize + br.bits(DIST_EXTRA[dsym] as u32)? as usize;
            if distance == 0 || distance > out.len() {
                return Err("distance exceeds output");
            }
            let start = out.len() - distance;
            for k in 0..len {
                let b = out[start + k];
                out.push(b);
            }
        }
        if out.len() > 1 << 31 {
            return Err("output too large");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_stored_block() {
        // zlib stream holding "hello" as a single stored block.
        let raw = b"hello";
        let mut z = vec![0x78, 0x01, 0x01, raw.len() as u8, 0x00, !(raw.len() as u8), 0xFF];
        z.extend_from_slice(raw);
        let (mut a, mut b) = (0u32, 0u32);
        for (i, &byte) in z.iter().enumerate() {
            a = a.wrapping_add(byte as u32);
            b = b.wrapping_add(a);
            if i == 1 {
                // header check is over the first two bytes only
            }
        }
        // append adler32 of raw
        let mut adler: u32 = 1;
        let (mut s1, mut s2) = (1u32, 0u32);
        for &byte in raw {
            s1 = (s1 + byte as u32) % 65521;
            s2 = (s2 + s1) % 65521;
        }
        adler = (s2 << 16) | s1;
        z.extend_from_slice(&adler.to_be_bytes());
        assert_eq!(zlib_inflate(&z).unwrap(), raw);
    }
}
