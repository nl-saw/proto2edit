//! Virtual filesystem over one or more mounted `.rcf` archives.
//!
//! The engine resolves an asset by hashing its path with the Cement hash and
//! binary-searching the archive index; the archives are mounted in load order
//! and later mounts win, which is also how a mod overlay replaces a shipped
//! asset. Paths are compared case-insensitively because the hash folds case.

use cement::rcf::{Rcf, RcfEntry};
use cement::rz;
use std::fmt;
use std::path::{Path, PathBuf};

/// A normalised asset path (backslash separated, lower case).
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct AssetPath(String);

impl AssetPath {
    /// Normalise a path: forward slashes become backslashes, leading
    /// separators are dropped and the whole path is lower-cased.
    pub fn new(path: &str) -> Self {
        let mut s = path.replace('/', "\\").to_lowercase();
        while s.starts_with('\\') {
            s.remove(0);
        }
        Self(s)
    }

    /// Borrow the normalised form.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for AssetPath {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Virtual filesystem failures.
#[derive(Debug)]
pub enum VfsError {
    /// No mounted archive contains the path.
    NotFound(String),
    /// The archive itself failed to open.
    Archive { path: PathBuf, source: cement::rcf::RcfError },
    /// The payload could not be read or decoded.
    Payload(String),
}

impl fmt::Display for VfsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            VfsError::NotFound(p) => write!(f, "asset not found: {p}"),
            VfsError::Archive { path, source } => {
                write!(f, "cannot mount {}: {source}", path.display())
            }
            VfsError::Payload(m) => write!(f, "payload error: {m}"),
        }
    }
}

impl std::error::Error for VfsError {}

/// One mounted archive, in mount order.
struct Mount {
    name: String,
    rcf: Rcf,
}

/// Archive overlay.
pub struct Vfs {
    mounts: Vec<Mount>,
}

impl Vfs {
    /// Create an empty filesystem.
    pub fn new() -> Self {
        Self { mounts: Vec::new() }
    }

    /// Mount every `.rcf` in a directory, sorted by name so the result is
    /// deterministic; later archives take precedence.
    pub fn mount_dir(&mut self, dir: impl AsRef<Path>) -> Result<usize, VfsError> {
        let mut paths: Vec<PathBuf> = std::fs::read_dir(dir.as_ref())
            .map_err(|e| VfsError::Payload(e.to_string()))?
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("rcf"))
            .collect();
        paths.sort();
        let mut n = 0;
        for p in paths {
            self.mount_file(&p)?;
            n += 1;
        }
        Ok(n)
    }

    /// Mount a single archive; later mounts shadow earlier ones.
    pub fn mount_file(&mut self, path: impl AsRef<Path>) -> Result<(), VfsError> {
        let path = path.as_ref();
        let rcf = Rcf::open(path).map_err(|source| VfsError::Archive {
            path: path.to_path_buf(),
            source,
        })?;
        let name = path.file_name().unwrap_or_default().to_string_lossy().into_owned();
        self.mounts.push(Mount { name, rcf });
        Ok(())
    }

    /// Number of mounted archives.
    pub fn archive_count(&self) -> usize {
        self.mounts.len()
    }

    /// Total entries across mounts.
    pub fn entry_count(&self) -> usize {
        self.mounts.iter().map(|m| m.rcf.entries.len()).sum()
    }

    /// Locate an asset, returning the owning mount index and entry.
    pub fn locate(&self, path: &AssetPath) -> Option<(usize, RcfEntry)> {
        for (i, m) in self.mounts.iter().enumerate().rev() {
            if let Some(e) = m.rcf.find(path.as_str()) {
                return Some((i, e.clone()));
            }
        }
        None
    }

    /// Whether an asset exists in the overlay.
    pub fn exists(&self, path: &AssetPath) -> bool {
        self.locate(path).is_some()
    }

    /// Read an asset, transparently unwrapping an `RZ` container.
    pub fn read(&mut self, path: &AssetPath) -> Result<Vec<u8>, VfsError> {
        let (mount, entry) = self
            .locate(path)
            .ok_or_else(|| VfsError::NotFound(path.to_string()))?;
        let raw = self.mounts[mount]
            .rcf
            .read(&entry)
            .map_err(|e| VfsError::Payload(e.to_string()))?;
        if rz::is_rz(&raw) {
            rz::unwrap_rz(&raw).map_err(|e| VfsError::Payload(e.to_string()))
        } else {
            Ok(raw)
        }
    }

    /// Read an asset that is expected to be Pure3D.
    pub fn read_p3d(&mut self, path: &AssetPath) -> Result<cement::p3d::Chunk, VfsError> {
        let data = self.read(path)?;
        cement::p3d::parse(&data).map_err(|e| VfsError::Payload(e.to_string()))
    }

    /// Iterate over every `(archive, entry)` pair in the overlay.
    pub fn iter_entries(&self) -> impl Iterator<Item = (&str, &RcfEntry)> {
        self.mounts
            .iter()
            .flat_map(|m| m.rcf.entries.iter().map(move |e| (m.name.as_str(), e)))
    }
}

impl Default for Vfs {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalises_paths() {
        assert_eq!(AssetPath::new("/Art/HUD/Main.gfx").as_str(), "art\\hud\\main.gfx");
        assert_eq!(AssetPath::new("art/hud/main.gfx"), AssetPath::new("ART\\HUD\\MAIN.GFX"));
    }
}
