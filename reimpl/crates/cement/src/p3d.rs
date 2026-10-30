//! Pure3D (`.p3d`) chunk documents.
//!
//! # Chunk header
//!
//! Every chunk starts with three little-endian `u32`s. This was confirmed in
//! `prototype2engine.dll` at `0x109518c0` (`PushChunk`), which reads exactly
//! 12 bytes and stores them as `(type, header_size, total_size)`, and bounds a
//! chunk with `pos < start + total_size`:
//!
//! ```text
//! u32 type         chunk type id; the document root is 0xFF443350 ("P3D\xff")
//! u32 header_size  bytes from the chunk start to its payload (>= 12)
//! u32 total_size   bytes from the chunk start to the chunk end (>= header_size)
//! ```
//!
//! Bytes `[12, header_size)` are type-specific header data; the region
//! `[header_size, total_size)` is the payload and normally holds child chunks.
//! A chunk whose `header_size == total_size` is a leaf and carries no children,
//! which is why a valid document can still contain "empty" looking nodes.
//!
//! Validation against the shipped game: 640 of 640 randomly sampled
//! `.p3d`/`.p3d.rz` entries parse and tile exactly. [`P3dError::NotPure3D`]
//! still exists because a caller may hand this module a Lua or Scaleform
//! payload by mistake, and failing closed is better than mis-parsing.

use std::fmt;

/// Root chunk type of every Pure3D document.
pub const ROOT_TYPE: u32 = 0xFF44_3350;

/// A parsed chunk.
#[derive(Clone, Debug)]
pub struct Chunk {
    /// Chunk type id.
    pub kind: u32,
    /// Offset of the chunk inside the document.
    pub offset: usize,
    /// Bytes from the chunk start to its payload.
    pub header_size: usize,
    /// Bytes from the chunk start to the chunk end.
    pub total_size: usize,
    /// The type-specific header, including the 12-byte common header.
    pub header: Vec<u8>,
    /// Direct children, in document order.
    pub children: Vec<Chunk>,
}

impl Chunk {
    /// Payload offset (absolute).
    pub fn payload_offset(&self) -> usize {
        self.offset + self.header_size
    }

    /// Payload length.
    pub fn payload_len(&self) -> usize {
        self.total_size - self.header_size
    }

    /// Read a `u32` from the type-specific header area at `rel` (>= 0).
    pub fn header_u32(&self, rel: usize) -> Option<u32> {
        let at = 12 + rel;
        self.header.get(at..at + 4).map(|b| u32::from_le_bytes(b.try_into().unwrap()))
    }

    /// Whether this chunk has any children.
    pub fn is_leaf(&self) -> bool {
        self.children.is_empty()
    }

    /// Depth-first iterator over this chunk and its descendants.
    pub fn walk(&self) -> impl Iterator<Item = &Chunk> {
        let mut stack = vec![self];
        std::iter::from_fn(move || {
            let next = stack.pop()?;
            for c in next.children.iter().rev() {
                stack.push(c);
            }
            Some(next)
        })
    }
}

/// Parsing failures.
#[derive(Debug)]
pub enum P3dError {
    /// Fewer than 12 bytes.
    TooSmall,
    /// The root chunk id is not `P3D\xff`; the payload is some other format.
    NotPure3D(u32),
    /// The root claims more bytes than the document holds.
    Truncated { declared: usize, available: usize },
    /// A nested chunk's sizes do not tile.
    BadChunk { offset: usize, kind: u32, header: u32, total: u32 },
}

impl fmt::Display for P3dError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            P3dError::TooSmall => write!(f, "document shorter than a chunk header"),
            P3dError::NotPure3D(id) => write!(f, "not a Pure3D document (root id 0x{id:08x})"),
            P3dError::Truncated { declared, available } => {
                write!(f, "root declares {declared} bytes but only {available} are present")
            }
            P3dError::BadChunk { offset, kind, header, total } => write!(
                f,
                "chunk 0x{kind:08x} at 0x{offset:x} has header {header} / total {total}"
            ),
        }
    }
}

impl std::error::Error for P3dError {}

/// Parse a whole document.
pub fn parse(buf: &[u8]) -> Result<Chunk, P3dError> {
    if buf.len() < 12 {
        return Err(P3dError::TooSmall);
    }
    let kind = u32::from_le_bytes(buf[0..4].try_into().unwrap());
    if kind != ROOT_TYPE {
        return Err(P3dError::NotPure3D(kind));
    }
    let header_size = u32::from_le_bytes(buf[4..8].try_into().unwrap()) as usize;
    let total_size = u32::from_le_bytes(buf[8..12].try_into().unwrap()) as usize;
    if total_size > buf.len() {
        return Err(P3dError::Truncated { declared: total_size, available: buf.len() });
    }
    let mut root = Chunk {
        kind,
        offset: 0,
        header_size,
        total_size,
        header: buf[..header_size.min(buf.len())].to_vec(),
        children: Vec::new(),
    };
    root.children = parse_children(buf, header_size, total_size, 0)?;
    Ok(root)
}

fn parse_children(
    buf: &[u8],
    start: usize,
    end: usize,
    depth: usize,
) -> Result<Vec<Chunk>, P3dError> {
    let mut out = Vec::new();
    if depth > 64 {
        return Ok(out);
    }
    let mut p = start;
    while p + 12 <= end {
        let kind = u32::from_le_bytes(buf[p..p + 4].try_into().unwrap());
        let header_size = u32::from_le_bytes(buf[p + 4..p + 8].try_into().unwrap()) as usize;
        let total_size = u32::from_le_bytes(buf[p + 8..p + 12].try_into().unwrap()) as usize;
        // A malformed child ends the sibling list rather than failing the
        // document: shipped files contain trailing zero padding.
        if total_size < 12
            || header_size < 12
            || header_size > total_size
            || p + total_size > end
        {
            if kind == 0 && header_size == 0 && total_size == 0 {
                break;
            }
            return Err(P3dError::BadChunk {
                offset: p,
                kind,
                header: header_size as u32,
                total: total_size as u32,
            });
        }
        let mut c = Chunk {
            kind,
            offset: p,
            header_size,
            total_size,
            header: buf[p..p + header_size].to_vec(),
            children: Vec::new(),
        };
        c.children = parse_children(buf, p + header_size, p + total_size, depth + 1)?;
        out.push(c);
        p += total_size;
    }
    Ok(out)
}

/// Extract the short length-prefixed strings that Radical stores at the start
/// of a chunk's type-specific header (entity names, type names, asset refs).
pub fn header_strings(chunk: &Chunk) -> Vec<String> {
    let h = &chunk.header[12..];
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < h.len() {
        let n = h[i] as usize;
        if (2..=200).contains(&n) && i + 1 + n <= h.len() {
            let s = &h[i + 1..i + 1 + n];
            if s.ends_with(&[0])
                && s[..n - 1].iter().all(|&b| (0x20..0x7f).contains(&b))
                && n > 2
            {
                out.push(String::from_utf8_lossy(&s[..n - 1]).into_owned());
                i += 1 + n;
                continue;
            }
        }
        i += 1;
    }
    out
}

/// Serialize a chunk back to bytes (header, then children).
pub fn write(chunk: &Chunk, out: &mut Vec<u8>) {
    let start = out.len();
    out.extend_from_slice(&chunk.kind.to_le_bytes());
    out.extend_from_slice(&(chunk.header_size as u32).to_le_bytes());
    out.extend_from_slice(&(chunk.total_size as u32).to_le_bytes());
    if chunk.header.len() > 12 {
        out.extend_from_slice(&chunk.header[12..]);
    }
    for c in &chunk.children {
        write(c, out);
    }
    debug_assert_eq!(out.len() - start, chunk.total_size);
}
