//! Payload identification.
//!
//! On this install the extensions are honest: all 25,806 `.p3d`/`.p3d.rz`
//! entries are Pure3D, the five `.lua` entries are Lua bytecode, and the 461
//! `.gfx` entries are Scaleform. Dispatching on content is still preferred
//! because it is one comparison and it survives a mod that renames a payload.

/// The payload formats present in the shipped install.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// Pure3D chunk document, root id `0xFF443350`.
    Pure3D,
    /// Lua 5.1 binary chunk (`\x1bLua\x51`).
    Lua51,
    /// Scaleform GFx movie (`CFX`).
    Scaleform,
    /// Bink video (`BIK`).
    Bink,
    /// Radical audio payload (`RADP`).
    Radp,
    /// UTF-8 text with a BOM.
    Text,
    /// Anything else.
    Unknown,
}

impl Kind {
    /// Identify a payload from its leading bytes.
    pub fn sniff(data: &[u8]) -> Kind {
        if data.starts_with(b"P3D") {
            Kind::Pure3D
        } else if data.starts_with(b"\x1bLua") {
            Kind::Lua51
        } else if data.starts_with(b"CFX") {
            Kind::Scaleform
        } else if data.starts_with(b"BIK") {
            Kind::Bink
        } else if data.starts_with(b"RADP") {
            Kind::Radp
        } else if data.starts_with(&[0xef, 0xbb, 0xbf]) {
            Kind::Text
        } else {
            Kind::Unknown
        }
    }

    /// Stable lower-case name, used by tooling output.
    pub fn name(self) -> &'static str {
        match self {
            Kind::Pure3D => "p3d",
            Kind::Lua51 => "lua51",
            Kind::Scaleform => "gfx",
            Kind::Bink => "bik",
            Kind::Radp => "radp",
            Kind::Text => "text",
            Kind::Unknown => "bin",
        }
    }
}

/// True when the extension does not describe the payload.
pub fn extension_mismatch(name: &str, kind: Kind) -> bool {
    let lower = name.to_lowercase();
    match kind {
        Kind::Pure3D => !(lower.ends_with(".p3d") || lower.ends_with(".p3d.rz")),
        Kind::Lua51 => !lower.ends_with(".lua"),
        Kind::Scaleform => !lower.ends_with(".gfx"),
        Kind::Bink => !lower.ends_with(".bik"),
        _ => false,
    }
}
