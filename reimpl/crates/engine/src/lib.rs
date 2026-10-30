//! `engine` — the beginning of an independent reimplementation of the
//! Prototype 2 runtime, built only on documented, observable behaviour.
//!
//! Scope of this crate today, and why:
//!
//! * [`vfs`] — mounts the shipped `.rcf` archives and resolves assets by the
//!   engine's own path hash. This is the piece everything else needs, and it is
//!   complete and tested against the real install.
//! * [`asset`] — identifies payload kinds (Pure3D, Lua bytecode, Scaleform,
//!   Bink, RADP audio, text) so the loader can dispatch the right parser.
//! * [`world`] — an entity/behaviour registry modelled on the engine's
//!   meta-object system, with a deterministic fixed-step tick.
//! * [`mission`] — the mission definition schema recovered from the shipped
//!   `scripts/missiondefinit.lua` bytecode, plus a readable authoring format so
//!   new missions can be written today without patching the game.
//!
//! What this crate deliberately does **not** claim: it cannot yet execute the
//! original Lua mission scripts, and it contains no renderer. Those are the
//! next milestones and are tracked in `docs/08-roadmap.md`.

pub mod asset;
pub mod mission;
pub mod vfs;
pub mod world;

pub use mission::{Mission, MissionError, Objective};
pub use vfs::{AssetPath, Vfs, VfsError};
pub use world::{Entity, EntityId, Registry, World};

/// Engine-wide result alias for the fallible entry points.
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
