//! Minimal deterministic world: entity storage, a behaviour registry and a
//! fixed-step tick.
//!
//! The shipped engine is built around a *meta-object* system: RTTI names in
//! `prototype2engine.dll` show a registry keyed by type name (`GameObject`,
//! `PropSpawnerBehaviourController`, `MatrixAttribute`, `PropInitData`, ...)
//! with attributes attached per object. This module reproduces that shape
//! without pretending to reproduce the original object layouts.

use std::collections::HashMap;

/// Generational-free entity handle.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct EntityId(pub u32);

/// A point-in-time entity: a type name and a bag of typed attributes.
#[derive(Clone, Debug, Default)]
pub struct Entity {
    /// Meta-object type name, e.g. `GameObject`.
    pub type_name: String,
    /// Source name recorded in the Pure3D document, if any.
    pub name: String,
    /// Attributes by name. Values keep their authored textual form so unknown
    /// types round-trip instead of being silently dropped.
    pub attributes: HashMap<String, String>,
    /// Parent entity, if this entity is nested.
    pub parent: Option<EntityId>,
}

impl Entity {
    /// Create an entity of the given type.
    pub fn new(type_name: impl Into<String>) -> Self {
        Self { type_name: type_name.into(), ..Default::default() }
    }

    /// Attach an attribute.
    pub fn with_attribute(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.attributes.insert(key.into(), value.into());
        self
    }
}

/// Behaviour factory signature: called once per entity that declares a
/// behaviour, returning its per-tick callback.
pub type BehaviourFactory = Box<dyn Fn(&Entity) -> Option<Box<dyn FnMut(&Entity, f32)>>>;

/// Type-name to factory map.
#[derive(Default)]
pub struct Registry {
    factories: HashMap<String, BehaviourFactory>,
}

impl Registry {
    /// Empty registry: every entity is inert.
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a behaviour for a meta-object type name.
    pub fn register(&mut self, type_name: impl Into<String>, factory: BehaviourFactory) {
        self.factories.insert(type_name.into(), factory);
    }

    /// Known type names.
    pub fn type_names(&self) -> impl Iterator<Item = &str> {
        self.factories.keys().map(String::as_str)
    }
}

/// Entity storage plus the fixed-step simulation loop.
pub struct World {
    entities: Vec<Entity>,
    behaviours: Vec<Option<Box<dyn FnMut(&Entity, f32)>>>,
    /// Accumulated simulation time in seconds.
    pub clock: f64,
    /// Fixed step used by [`World::advance`].
    pub step: f32,
    /// Time carried over between [`World::advance`] calls so no time is lost
    /// to fixed-step quantisation.
    accumulator: f32,
}

impl World {
    /// Create an empty world with a 1/60 s step.
    pub fn new() -> Self {
        Self {
            entities: Vec::new(),
            behaviours: Vec::new(),
            clock: 0.0,
            step: 1.0 / 60.0,
            accumulator: 0.0,
        }
    }

    /// Insert an entity, returning its handle.
    pub fn spawn(&mut self, entity: Entity) -> EntityId {
        let id = EntityId(self.entities.len() as u32);
        self.entities.push(entity);
        self.behaviours.push(None);
        id
    }

    /// Number of live entities.
    pub fn len(&self) -> usize {
        self.entities.len()
    }

    /// Whether the world is empty.
    pub fn is_empty(&self) -> bool {
        self.entities.is_empty()
    }

    /// Borrow an entity.
    pub fn get(&self, id: EntityId) -> Option<&Entity> {
        self.entities.get(id.0 as usize)
    }

    /// Bind behaviours to every entity using the registry. Entities whose type
    /// is unknown stay inert, which mirrors how the shipped engine skips
    /// meta-objects whose handler is not loaded.
    pub fn bind(&mut self, registry: &Registry) -> usize {
        let mut bound = 0;
        for (i, e) in self.entities.iter().enumerate() {
            if let Some(f) = registry.factories.get(&e.type_name) {
                if let Some(cb) = f(e) {
                    self.behaviours[i] = Some(cb);
                    bound += 1;
                }
            }
        }
        bound
    }

    /// Advance by `dt` seconds in fixed steps and return the number of steps
    /// actually simulated. Leftover time below one step is carried into the
    /// next call.
    pub fn advance(&mut self, dt: f32) -> u32 {
        self.accumulator += dt.max(0.0);
        let mut steps = 0;
        while self.accumulator >= self.step {
            self.tick(self.step);
            self.accumulator -= self.step;
            steps += 1;
        }
        steps
    }

    /// Time carried but not yet simulated.
    pub fn pending_time(&self) -> f32 {
        self.accumulator
    }

    fn tick(&mut self, dt: f32) {
        self.clock += f64::from(dt);
        for (i, cb) in self.behaviours.iter_mut().enumerate() {
            if let Some(f) = cb.as_mut() {
                // Split borrow: entity data is read-only during a tick.
                f(&self.entities[i], dt);
            }
        }
    }
}

impl Default for World {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_step_advance_carries_remainder() {
        let mut w = World::new();
        w.spawn(Entity::new("GameObject"));
        // 0.1 s at 1/60 s per step is 6 steps with ~0.0000017 s carried.
        assert_eq!(w.advance(0.1), 6);
        assert!((w.clock - 0.1).abs() < 0.002);
        // Two sub-step calls accumulate into exactly one step.
        let mut w = World::new();
        assert_eq!(w.advance(0.008), 0);
        assert_eq!(w.advance(0.009), 1);
        assert!(w.pending_time() < w.step);
    }

    #[test]
    fn unknown_types_stay_inert() {
        let mut w = World::new();
        w.spawn(Entity::new("NotARegisteredType"));
        let reg = Registry::new();
        assert_eq!(w.bind(&reg), 0);
    }
}
