//! Name hashing used by the Cement engine.
//!
//! `hash_filename` reproduces the function that the engine uses to key RCF
//! directory entries. It was recovered from the community file-format
//! description and then validated against the shipped data: applying it to
//! every name stored in every Prototype 2 archive yields exactly the hash set
//! present in that archive's sorted index (see `tools/rcf.py verify`).

/// Hash a path the way the engine does.
///
/// A leading backslash is stripped. The accumulator is multiplied by 31 per
/// byte, then incremented by the byte; bytes below `'a'` are folded up by
/// `0x20` so the hash is effectively case-insensitive for letters and stable
/// for the separators the engine uses.
pub fn hash_filename(name: &str) -> u32 {
    let mut seed: u32 = 0;
    let bytes = name.strip_prefix('\\').unwrap_or(name).as_bytes();
    for &b in bytes {
        seed = seed.wrapping_mul(31);
        seed = seed.wrapping_add(if b < 0x61 { b as u32 + 0x20 } else { b as u32 });
    }
    seed
}

#[cfg(test)]
mod tests {
    use super::hash_filename;

    #[test]
    fn known_hashes() {
        // Values read straight out of boot.rcf's sorted index; the nine
        // art\hud\*.gfx names in that archive map 1:1 onto its nine CFX files.
        assert_eq!(hash_filename("art\\hud\\main.gfx"), 0xc5f44002);
        assert_eq!(hash_filename("scripts\\init.lua"), 0xf05c9a6e);
        assert_eq!(hash_filename("art\\hud\\list.gfx"), 0x16a9f067);
        assert_eq!(hash_filename("art\\hud\\fonts_latin.gfx"), 0x78630cb6);
    }

    #[test]
    fn case_insensitive() {
        assert_eq!(hash_filename("ART\\HUD\\MAIN.GFX"), hash_filename("art\\hud\\main.gfx"));
        assert_eq!(hash_filename("\\art\\hud\\main.gfx"), hash_filename("art\\hud\\main.gfx"));
    }
}
