//! Mission definitions.
//!
//! The shipped mission layer lives in `scripts/missiondefinit.lua` (221 KB of
//! Lua 5.1 bytecode inside `boot.rcf`). Its string constants and globals give
//! the authoring schema directly:
//!
//! * `mm_MissionFlowConfigs` — the mission table,
//! * `mm_MissionTable`, `mm_StreamGroupDefs`, `mm_TagSetGroupDefs`,
//! * `Preprocess{Open,Activation,Package,Completion}Data`,
//! * `mms_MakeListASublist`, `mms_AddStandardFieldsToConfigTable`, `mms_Error`,
//! * fields `MISSION_NAME`, `MISSION_UNLOCK_KEY`, `MISSION_NAME_UID`,
//!   `MISSION_MANAGEMENT_TYPE`, `MISSION_OBJECT_TAG`,
//!   `DEBUG_START_POSITION`, `DEBUG_START_ROTATION`, `pipData`,
//! * asset groups `openList`, `activeList`, `activePreloadList`,
//!   `activeCharacterList`, `openData`, `activationData`, `packageData`,
//!   `scopingTriggers`,
//! * diagnostics such as `"'Background' mission cannot specify openList assets"`.
//!
//! This module models that schema and reads a small line-oriented authoring
//! format so missions can be written without touching the shipped scripts.

use std::collections::BTreeMap;
use std::fmt;

/// Why a mission file could not be parsed.
#[derive(Debug)]
pub struct MissionError {
    /// 1-based line number.
    pub line: usize,
    /// Human-readable reason.
    pub message: String,
}

impl fmt::Display for MissionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "line {}: {}", self.line, self.message)
    }
}

impl std::error::Error for MissionError {}

/// How a mission participates in world streaming, matching the shipped
/// `MISSION_MANAGEMENT_TYPE` values.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ManagementType {
    /// Mission drives its own open/active asset sets.
    Scenario,
    /// Always-streamed ambient content; cannot request mission assets.
    Background,
}

impl ManagementType {
    fn parse(s: &str) -> Option<Self> {
        match s.to_lowercase().as_str() {
            "scenario" | "standard" => Some(Self::Scenario),
            "background" => Some(Self::Background),
            _ => None,
        }
    }
}

/// One objective inside a mission.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Objective {
    /// Objective tag, matching `MISSION_OBJECT_TAG` usage.
    pub tag: String,
    /// Player-facing text or text-bible key.
    pub text: String,
    /// Optional time limit in seconds.
    pub time_limit: Option<u32>,
}

/// A mission definition.
#[derive(Clone, Debug)]
pub struct Mission {
    /// `MISSION_NAME`.
    pub name: String,
    /// `MISSION_UNLOCK_KEY`.
    pub unlock_key: String,
    /// `MISSION_NAME_UID`, used for localisation lookup.
    pub name_uid: String,
    /// `MISSION_MANAGEMENT_TYPE`.
    pub management: ManagementType,
    /// `location` field.
    pub location: Option<String>,
    /// `openList`: assets streamed while the mission is offered in the world.
    pub open_list: Vec<String>,
    /// `activeList`: assets streamed while the mission runs.
    pub active_list: Vec<String>,
    /// `activePreloadList`: assets preloaded before the active phase.
    pub active_preload_list: Vec<String>,
    /// Objectives, in order.
    pub objectives: Vec<Objective>,
    /// Free-form key/value extras preserved for forward compatibility.
    pub extras: BTreeMap<String, String>,
}

impl Default for Mission {
    fn default() -> Self {
        Self {
            name: String::new(),
            unlock_key: String::new(),
            name_uid: String::new(),
            management: ManagementType::Scenario,
            location: None,
            open_list: Vec::new(),
            active_list: Vec::new(),
            active_preload_list: Vec::new(),
            objectives: Vec::new(),
            extras: BTreeMap::new(),
        }
    }
}

impl Mission {
    /// Parse the authoring format.
    ///
    /// ```text
    /// mission gz_fi_01
    ///   management scenario
    ///   unlock_key gz_fi_01
    ///   name_uid MISSION_NAME_GZ_FI_01
    ///   location green_zone
    ///   open_list    art/locations/green_zone/Cell_0.p3d.rz
    ///   active_list  art/packages/missions/flyer/flyer.p3d
    ///   objective "talk_to_priest" "Talk to the priest"
    ///   objective "reach_lair"    "Reach the lair" 120
    ///   # comments and blank lines are ignored
    /// ```
    pub fn parse(text: &str) -> Result<Self, MissionError> {
        let mut m = Mission::default();
        let mut started = false;
        for (idx, raw) in text.lines().enumerate() {
            let line = idx + 1;
            let t = raw.trim();
            if t.is_empty() || t.starts_with('#') || t.starts_with("//") {
                continue;
            }
            let (key, rest) = match t.split_once(char::is_whitespace) {
                Some((k, r)) => (k, r.trim()),
                None => (t, ""),
            };
            let err = |msg: &str| MissionError { line, message: msg.to_string() };
            match key {
                "mission" => {
                    if started {
                        return Err(err("only one mission per file"));
                    }
                    if rest.is_empty() {
                        return Err(err("mission needs a name"));
                    }
                    m.name = rest.to_string();
                    started = true;
                }
                "management" => {
                    m.management = ManagementType::parse(rest)
                        .ok_or_else(|| err("management must be 'scenario' or 'background'"))?;
                }
                "unlock_key" => m.unlock_key = rest.to_string(),
                "name_uid" => m.name_uid = rest.to_string(),
                "location" => m.location = Some(rest.to_string()),
                "open_list" => push_asset(&mut m.open_list, rest),
                "active_list" => push_asset(&mut m.active_list, rest),
                "active_preload_list" => push_asset(&mut m.active_preload_list, rest),
                "objective" => {
                    let quoted = split_quoted(rest);
                    if quoted.len() < 2 {
                        return Err(err("objective needs \"tag\" \"text\" [seconds]"));
                    }
                    let time_limit = quoted.get(2).and_then(|s| s.parse::<u32>().ok());
                    m.objectives.push(Objective {
                        tag: quoted[0].clone(),
                        text: quoted[1].clone(),
                        time_limit,
                    });
                }
                other => {
                    m.extras.insert(other.to_string(), rest.to_string());
                }
            }
        }
        if !started {
            return Err(MissionError { line: 1, message: "missing `mission <name>` header".into() });
        }
        // The shipped scripts enforce the same rule, with the same wording.
        if m.management == ManagementType::Background && !m.open_list.is_empty() {
            return Err(MissionError {
                line: 1,
                message: "'Background' mission cannot specify openList assets".into(),
            });
        }
        Ok(m)
    }

    /// Render the mission back to the authoring format.
    pub fn to_text(&self) -> String {
        let mut out = format!("mission {}\n", self.name);
        out.push_str(&format!(
            "  management {}\n",
            match self.management {
                ManagementType::Scenario => "scenario",
                ManagementType::Background => "background",
            }
        ));
        if !self.unlock_key.is_empty() {
            out.push_str(&format!("  unlock_key {}\n", self.unlock_key));
        }
        if !self.name_uid.is_empty() {
            out.push_str(&format!("  name_uid {}\n", self.name_uid));
        }
        if let Some(l) = &self.location {
            out.push_str(&format!("  location {l}\n"));
        }
        for a in &self.open_list {
            out.push_str(&format!("  open_list {a}\n"));
        }
        for a in &self.active_list {
            out.push_str(&format!("  active_list {a}\n"));
        }
        for a in &self.active_preload_list {
            out.push_str(&format!("  active_preload_list {a}\n"));
        }
        for o in &self.objectives {
            match o.time_limit {
                Some(t) => out.push_str(&format!("  objective \"{}\" \"{}\" {}\n", o.tag, o.text, t)),
                None => out.push_str(&format!("  objective \"{}\" \"{}\"\n", o.tag, o.text)),
            }
        }
        for (k, v) in &self.extras {
            out.push_str(&format!("  {k} {v}\n"));
        }
        out
    }
}

fn push_asset(list: &mut Vec<String>, value: &str) {
    for part in value.split(',') {
        let p = part.trim();
        if !p.is_empty() {
            list.push(p.to_string());
        }
    }
}

fn split_quoted(input: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut quoted = false;
    for c in input.chars() {
        match c {
            '"' => {
                if quoted {
                    out.push(std::mem::take(&mut cur));
                    quoted = false;
                } else {
                    quoted = true;
                }
            }
            c if c.is_whitespace() && !quoted => {
                if !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                }
            }
            c => cur.push(c),
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "\
mission gz_fi_01
  management scenario
  unlock_key gz_fi_01
  name_uid MISSION_NAME_GZ_FI_01
  location green_zone
  open_list  art/locations/green_zone/Cell_0.p3d.rz
  active_list art/packages/missions/flyer/flyer.p3d
  objective \"talk_to_priest\" \"Talk to the priest\"
  objective \"reach_lair\" \"Reach the lair\" 120
";

    #[test]
    fn parses_and_round_trips() {
        let m = Mission::parse(SAMPLE).unwrap();
        assert_eq!(m.name, "gz_fi_01");
        assert_eq!(m.management, ManagementType::Scenario);
        assert_eq!(m.objectives.len(), 2);
        assert_eq!(m.objectives[1].time_limit, Some(120));
        let again = Mission::parse(&m.to_text()).unwrap();
        assert_eq!(again.name, m.name);
        assert_eq!(again.objectives.len(), m.objectives.len());
    }

    #[test]
    fn background_missions_cannot_have_open_assets() {
        let src = "mission bg\n  management background\n  open_list x.p3d\n";
        let err = Mission::parse(src).unwrap_err();
        assert!(err.message.contains("cannot specify openList"));
    }

    // The worked example that used to live in mods/example-mission/, kept inline so the parser is exercised
    // against a complete mission rather than only the tiny fixtures around it.
    const EXAMPLE_MISSION: &str = "\
mission gz_test_01
  management scenario
  unlock_key gz_test_01
  name_uid MISSION_NAME_GZ_TEST_01
  location green_zone
  open_list    art/locations/green_zone/Cell_0.p3d.rz
  active_list  art/packages/missions/flyer/flyer.p3d
  objective \"talk_to_priest\" \"Talk to the priest\"
  objective \"reach_lair\" \"Reach the lair\" 120
";

    #[test]
    fn complete_example_mission_parses() {
        let m = Mission::parse(EXAMPLE_MISSION).unwrap();
        assert_eq!(m.name, "gz_test_01");
        assert_eq!(m.objectives.len(), 2);
        assert_eq!(m.open_list.len(), 1);
    }

    #[test]
    fn needs_a_header() {
        assert!(Mission::parse("  management scenario\n").is_err());
    }
}
