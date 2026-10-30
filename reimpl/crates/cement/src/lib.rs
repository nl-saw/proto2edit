//! `cement` — container formats of Radical Entertainment's Cement engine as
//! shipped with *Prototype 2*.
//!
//! Everything here is derived from the shipped binaries and data:
//!
//! * [`rcf`] — `ATG CORE CEMENT LIBRARY` archives, including the name hash
//!   (verified to map all 26,385 shipped entries with zero collisions),
//! * [`p3d`] — Pure3D chunk documents, whose three-word header
//!   (`type`, `header_size`, `total_size`) was confirmed against
//!   `prototype2engine.dll` at `0x109518c0`,
//! * [`rz`] — the `RZ` zlib wrapper used for `.p3d.rz` payloads,
//! * [`lua51`] — a Lua 5.1 bytecode undumper (the engine was built with
//!   `lua_Number == float`, so the undumper honours the chunk header).
//!
//! The crate is dependency-free on purpose: it must be usable as the asset
//! layer of an independent reimplementation without licensing surprises.

pub mod hash;
pub mod lua51;
pub mod p3d;
pub mod rcf;
pub mod rz;

pub use hash::hash_filename;
pub use rcf::{Rcf, RcfEntry};
pub use rz::unwrap_rz;
