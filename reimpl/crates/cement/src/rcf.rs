//! `ATG CORE CEMENT LIBRARY` archives (`.rcf`).
//!
//! # Layout (little-endian)
//!
//! ```text
//! header (0x3C)
//!   0x00 char[24]  "ATG CORE CEMENT LIBRARY" + zero padding
//!   0x18 u32[2]    zero padding
//!   0x20 u8 major, u8 minor, u8 endian_flag, u8 unknown1
//!   0x24 u32 index_offset   (0x3C)
//!   0x28 u32 index_size     (count * 12)
//!   0x2C u32 metadata_offset (2048-aligned)
//!   0x30 u32 metadata_size
//!   0x34 u32 reserved
//!   0x38 u32 count
//! index: count * { u32 hash, u32 offset, u32 size } sorted by hash
//! metadata: u32 alignment, u32 reserved, then count * {
//!     u32 type_hash, u32 alignment, u32 reserved, u32 name_len,
//!     char name[name_len] (includes NUL), u8 reserved[3] }
//! ```
//!
//! The metadata records are stored in authoring order while the index is
//! sorted by hash, so a record is associated with its payload by hashing the
//! name ([`crate::hash::hash_filename`]), never by position.

use crate::hash::hash_filename;
use std::collections::HashMap;
use std::fmt;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

const MAGIC: &[u8; 24] = b"ATG CORE CEMENT LIBRARY\0";
const HEADER_SIZE: usize = 0x3C;

/// Archive read/format failures.
#[derive(Debug)]
pub enum RcfError {
    Io(std::io::Error),
    /// The file does not start with the Cement magic.
    BadMagic,
    /// The format version was not 2.1.
    UnsupportedVersion(u8, u8),
    /// Big-endian archives exist (console builds) but are not handled here.
    UnsupportedEndian(u8),
    /// A table or payload range falls outside the file.
    OutOfRange(&'static str),
    /// Index size disagrees with `count * 12`.
    BadIndexSize { count: u32, size: u32 },
    /// A metadata record's name length is implausible.
    BadName { offset: usize, len: u32 },
}

impl fmt::Display for RcfError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RcfError::Io(e) => write!(f, "i/o error: {e}"),
            RcfError::BadMagic => write!(f, "not a Cement archive"),
            RcfError::UnsupportedVersion(a, b) => write!(f, "unsupported version {a}.{b}"),
            RcfError::UnsupportedEndian(v) => write!(f, "unsupported endian flag {v}"),
            RcfError::OutOfRange(what) => write!(f, "{what} out of range"),
            RcfError::BadIndexSize { count, size } => {
                write!(f, "index size {size} does not fit {count} entries")
            }
            RcfError::BadName { offset, len } => {
                write!(f, "implausible name length {len} at metadata offset 0x{offset:x}")
            }
        }
    }
}

impl std::error::Error for RcfError {}

impl From<std::io::Error> for RcfError {
    fn from(e: std::io::Error) -> Self {
        RcfError::Io(e)
    }
}

/// One file inside an archive.
#[derive(Clone, Debug)]
pub struct RcfEntry {
    /// Hash of the file name.
    pub hash: u32,
    /// Absolute offset of the payload.
    pub offset: u32,
    /// Payload length in bytes.
    pub size: u32,
    /// Authoring order index in the metadata table.
    pub metadata_index: usize,
    /// File name, backslash separated, as authored.
    pub name: String,
    /// Per-file alignment, 2048 in every shipped archive.
    pub alignment: u32,
    /// Metadata type hash, always 0 in shipped Prototype 2 archives.
    pub type_hash: u32,
}

/// A memory-mapped-ish reader over an `.rcf` file.
pub struct Rcf {
    file: File,
    file_size: u64,
    pub major: u8,
    pub minor: u8,
    pub count: u32,
    /// Entries sorted by name for human listing.
    pub entries: Vec<RcfEntry>,
    by_hash: HashMap<u32, usize>,
}

impl Rcf {
    /// Open and index an archive.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, RcfError> {
        let path = path.as_ref();
        let mut file = File::open(path)?;
        let file_size = file.metadata()?.len();
        let mut head = [0u8; HEADER_SIZE];
        file.read_exact(&mut head)?;
        if &head[..24] != MAGIC {
            return Err(RcfError::BadMagic);
        }
        let (major, minor, endian_flag) = (head[0x20], head[0x21], head[0x22]);
        if (major, minor) != (2, 1) {
            return Err(RcfError::UnsupportedVersion(major, minor));
        }
        if endian_flag != 0 {
            return Err(RcfError::UnsupportedEndian(endian_flag));
        }
        let rd32 = |o: usize| u32::from_le_bytes(head[o..o + 4].try_into().unwrap());
        let index_offset = rd32(0x24);
        let index_size = rd32(0x28);
        let metadata_offset = rd32(0x2C);
        let metadata_size = rd32(0x30);
        let count = rd32(0x38);

        if index_size != count.saturating_mul(12) {
            return Err(RcfError::BadIndexSize { count, size: index_size });
        }
        if u64::from(index_offset) + u64::from(index_size) > file_size {
            return Err(RcfError::OutOfRange("index"));
        }
        if u64::from(metadata_offset) + u64::from(metadata_size) > file_size {
            return Err(RcfError::OutOfRange("metadata"));
        }

        let mut index = vec![0u8; index_size as usize];
        file.seek(SeekFrom::Start(u64::from(index_offset)))?;
        file.read_exact(&mut index)?;
        let mut metadata = vec![0u8; metadata_size as usize];
        file.seek(SeekFrom::Start(u64::from(metadata_offset)))?;
        file.read_exact(&mut metadata)?;

        // Index: (hash, offset, size), sorted by hash.
        let mut by_hash: HashMap<u32, usize> = HashMap::with_capacity(count as usize);
        let mut entries: Vec<RcfEntry> = Vec::with_capacity(count as usize);
        for i in 0..count as usize {
            let base = i * 12;
            let hash = u32::from_le_bytes(index[base..base + 4].try_into().unwrap());
            let offset = u32::from_le_bytes(index[base + 4..base + 8].try_into().unwrap());
            let size = u32::from_le_bytes(index[base + 8..base + 12].try_into().unwrap());
            entries.push(RcfEntry {
                hash,
                offset,
                size,
                metadata_index: usize::MAX,
                name: String::new(),
            alignment: 0,
            type_hash: 0,
            });
            by_hash.insert(hash, i);
        }

        // Metadata: associate by hash.
        let mut p = 8usize; // skip {alignment, reserved}
        for _ in 0..count {
            if p + 16 > metadata.len() {
                return Err(RcfError::OutOfRange("metadata record"));
            }
            let type_hash = u32::from_le_bytes(metadata[p..p + 4].try_into().unwrap());
            let alignment = u32::from_le_bytes(metadata[p + 4..p + 8].try_into().unwrap());
            let name_len = u32::from_le_bytes(metadata[p + 12..p + 16].try_into().unwrap());
            let start = p + 16;
            if name_len == 0 || name_len > 4096 || start + name_len as usize > metadata.len() {
                return Err(RcfError::BadName { offset: p, len: name_len });
            }
            let raw = &metadata[start..start + name_len as usize];
            let end = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
            let name = String::from_utf8_lossy(&raw[..end]).into_owned();
            if let Some(&i) = by_hash.get(&hash_filename(&name)) {
                entries[i].name = name;
                entries[i].alignment = alignment;
                entries[i].type_hash = type_hash;
            }
            p = start + name_len as usize + 3;
        }

        entries.sort_by(|a, b| a.name.cmp(&b.name));
        // `by_hash` stored the *pre-sort* index, so after ordering the vector every lookup pointed at an
        // unrelated entry. Rebuild it against the sorted vector; the map is the only index find()/read_name()
        // (and engine::Vfs) trust.
        let mut by_hash: HashMap<u32, usize> = HashMap::with_capacity(entries.len());
        for (i, entry) in entries.iter().enumerate() {
            by_hash.insert(entry.hash, i);
        }
        Ok(Self { file, file_size, major, minor, count, entries, by_hash })
    }

    /// Total byte length of the archive.
    pub fn file_size(&self) -> u64 {
        self.file_size
    }

    /// Look up an entry by path, using the engine's hash.
    pub fn find(&self, name: &str) -> Option<&RcfEntry> {
        self.by_hash
            .get(&hash_filename(name))
            .map(|&i| &self.entries[i])
            .filter(|e| !e.name.is_empty())
    }

    /// Read one entry's payload.
    pub fn read(&mut self, entry: &RcfEntry) -> Result<Vec<u8>, RcfError> {
        if u64::from(entry.offset) + u64::from(entry.size) > self.file_size {
            return Err(RcfError::OutOfRange("payload"));
        }
        let mut buf = vec![0u8; entry.size as usize];
        self.file.seek(SeekFrom::Start(u64::from(entry.offset)))?;
        self.file.read_exact(&mut buf)?;
        Ok(buf)
    }

    /// Read an entry by name, returning `None` when it is absent.
    pub fn read_name(&mut self, name: &str) -> Result<Option<Vec<u8>>, RcfError> {
        let Some(entry) = self.find(name).cloned() else {
            return Ok(None);
        };
        self.read(&entry).map(Some)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_non_cement() {
        let dir = std::env::temp_dir();
        let p = dir.join("cement_not_an_archive.bin");
        std::fs::write(&p, b"not an archive at all").unwrap();
        assert!(matches!(Rcf::open(&p), Err(RcfError::Io(_)) | Err(RcfError::BadMagic)));
        let _ = std::fs::remove_file(p);
    }
}
