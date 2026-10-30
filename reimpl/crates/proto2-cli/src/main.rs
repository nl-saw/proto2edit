//! `proto2` — command line front end for the `cement` readers.
//!
//! All commands are read-only against the game install; extraction writes
//! into a caller-chosen directory.

use cement::{hash_filename, lua51, p3d, rcf::Rcf, rz};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const USAGE: &str = "\
proto2 — Prototype 2 asset tooling

USAGE:
  proto2 hash <name>
  proto2 list <archive.rcf> [--filter <substr>]
  proto2 verify <archive.rcf>
  proto2 extract <archive.rcf> <outdir> [--filter <substr>] [--rz]
  proto2 p3d <file.p3d> [tree|strings|stats|validate]
  proto2 lua <file.lua> [stats|strings|globals|tree]
  proto2 mount <game-dir>
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let Some(cmd) = args.first() else {
        print!("{USAGE}");
        return Ok(());
    };
    match cmd.as_str() {
        "hash" => {
            let name = args.get(1).ok_or("hash needs a name")?;
            println!("0x{:08x}  {}", hash_filename(name), name);
        }
        "list" => {
            let path = args.get(1).ok_or("list needs an archive")?;
            let filter = flag(args, "--filter");
            let rcf = Rcf::open(path)?;
            let mut n = 0usize;
            for e in &rcf.entries {
                if let Some(f) = &filter {
                    if !e.name.to_lowercase().contains(&f.to_lowercase()) {
                        continue;
                    }
                }
                println!("{:>10}  0x{:08x}  {}", e.size, e.hash, e.name);
                n += 1;
            }
            eprintln!("-- {n}/{} entries, v{}.{}", rcf.count, rcf.major, rcf.minor);
        }
        "verify" => {
            let path = args.get(1).ok_or("verify needs an archive")?;
            let rcf = Rcf::open(path)?;
            let unnamed = rcf.entries.iter().filter(|e| e.name.is_empty()).count();
            let overrun = rcf
                .entries
                .iter()
                .filter(|e| u64::from(e.offset) + u64::from(e.size) > rcf.file_size())
                .count();
            let misaligned = rcf
                .entries
                .iter()
                .filter(|e| e.alignment != 0 && e.offset % e.alignment != 0)
                .count();
            println!("archive          {path}");
            println!("version          {}.{}", rcf.major, rcf.minor);
            println!("entries          {}", rcf.count);
            println!("named            {}", rcf.count as usize - unnamed);
            println!("unnamed entries  {unnamed}");
            println!("overrunning      {overrun}");
            println!("misaligned       {misaligned}");
            if unnamed != 0 || overrun != 0 {
                return Err("archive failed integrity checks".into());
            }
        }
        "extract" => {
            let path = args.get(1).ok_or("extract needs an archive")?;
            let outdir = PathBuf::from(args.get(2).ok_or("extract needs an output directory")?);
            let filter = flag(args, "--filter");
            let do_rz = args.iter().any(|a| a == "--rz");
            let mut rcf = Rcf::open(path)?;
            let mut written = 0usize;
            for e in rcf.entries.clone() {
                if let Some(f) = &filter {
                    if !e.name.to_lowercase().contains(&f.to_lowercase()) {
                        continue;
                    }
                }
                let raw = rcf.read(&e)?;
                let data = if do_rz { rz::unwrap_rz(&raw)? } else { raw };
                let rel = e.name.replace('\\', "/");
                let dest = outdir.join(&rel);
                if let Some(parent) = dest.parent() {
                    fs::create_dir_all(parent)?;
                }
                fs::write(&dest, &data)?;
                println!("{:>10}  {}", data.len(), rel);
                written += 1;
            }
            eprintln!("-- wrote {written} file(s)");
        }
        "p3d" => {
            let path = args.get(1).ok_or("p3d needs a file")?;
            let mode = args.get(2).map(String::as_str).unwrap_or("tree");
            let buf = fs::read(path)?;
            let root = p3d::parse(&buf)?;
            match mode {
                "tree" => {
                    fn dump(c: &p3d::Chunk, ind: usize) {
                        let s = p3d::header_strings(c);
                        let tag = if s.is_empty() { String::new() } else { format!("  {s:?}") };
                        println!(
                            "{:ind$}0x{:08x} hdr={} total={} @0x{:x}{}",
                            "",
                            c.kind,
                            c.header_size,
                            c.total_size,
                            c.offset,
                            tag,
                            ind = ind * 2
                        );
                        for k in &c.children {
                            dump(k, ind + 1);
                        }
                    }
                    dump(&root, 0);
                }
                "strings" => {
                    for c in root.walk() {
                        for s in p3d::header_strings(c) {
                            println!("{s}");
                        }
                    }
                }
                "stats" => {
                    use std::collections::BTreeMap;
                    let mut kinds: BTreeMap<u32, usize> = BTreeMap::new();
                    let mut n = 0usize;
                    for c in root.walk() {
                        *kinds.entry(c.kind).or_default() += 1;
                        n += 1;
                    }
                    println!("chunks {n}  distinct {}", kinds.len());
                    let mut v: Vec<_> = kinds.into_iter().collect();
                    v.sort_by_key(|(_, n)| std::cmp::Reverse(*n));
                    for (k, n) in v {
                        println!("  0x{k:08x}  {n}");
                    }
                }
                "validate" => {
                    let n = root.walk().count();
                    println!("ok chunks={n} bytes={}", buf.len());
                }
                other => return Err(format!("unknown p3d mode {other}").into()),
            }
        }
        "lua" => {
            let path = args.get(1).ok_or("lua needs a file")?;
            let mode = args.get(2).map(String::as_str).unwrap_or("stats");
            let data = fs::read(path)?;
            let root = lua51::load(&data)?;
            match mode {
                "stats" => {
                    let protos = root.walk().count();
                    let instructions: usize = root.walk().map(|p| p.code.len()).sum();
                    let strings: usize = root.walk().map(|p| p.strings().count()).sum();
                    println!("protos            {protos}");
                    println!("instructions      {instructions}");
                    println!("string constants  {strings}");
                }
                "strings" => {
                    let mut seen = std::collections::BTreeSet::new();
                    for p in root.walk() {
                        for s in p.strings() {
                            let t = String::from_utf8_lossy(s).into_owned();
                            if seen.insert(t.clone()) {
                                println!("{t}");
                            }
                        }
                    }
                }
                "globals" => {
                    let mut seen = std::collections::BTreeSet::new();
                    for p in root.walk() {
                        for word in &p.code {
                            let ins = lua51::decode(*word);
                            if lua51::OPCODES[ins.op as usize] == "GETGLOBAL" {
                                if let Some(cement::lua51::Constant::Str(s)) =
                                    p.constants.get(ins.bx as usize)
                                {
                                    seen.insert(String::from_utf8_lossy(s).into_owned());
                                }
                            }
                        }
                    }
                    for s in seen {
                        println!("{s}");
                    }
                }
                "tree" => {
                    fn t(p: &lua51::Proto, d: usize) {
                        println!(
                            "{:ind$}lines {}-{}  protos={}  consts={}",
                            "",
                            p.line_defined,
                            p.last_line,
                            p.protos.len(),
                            p.constants.len(),
                            ind = d * 2
                        );
                        for c in &p.protos {
                            t(c, d + 1);
                        }
                    }
                    t(&root, 0);
                }
                other => return Err(format!("unknown lua mode {other}").into()),
            }
        }
        "mount" => {
            let dir = Path::new(args.get(1).ok_or("mount needs the game directory")?);
            let mut total = 0u64;
            let mut entries = 0usize;
            let mut archives = Vec::new();
            for entry in fs::read_dir(dir)? {
                let entry = entry?;
                let path = entry.path();
                if path.extension().and_then(|e| e.to_str()) != Some("rcf") {
                    continue;
                }
                match Rcf::open(&path) {
                    Ok(rcf) => {
                        archives.push((path, rcf.count, rcf.file_size()));
                        entries += rcf.count as usize;
                        total += rcf.file_size();
                    }
                    Err(e) => eprintln!("skip {}: {e}", path.display()),
                }
            }
            archives.sort_by(|a, b| a.0.cmp(&b.0));
            for (p, count, size) in &archives {
                println!("{:>6} entries  {:>12} bytes  {}", count, size, p.file_name().unwrap().to_string_lossy());
            }
            println!("-- {} archives, {entries} entries, {:.2} GiB", archives.len(), total as f64 / (1u64 << 30) as f64);
        }
        other => return Err(format!("unknown command {other}\n\n{USAGE}").into()),
    }
    Ok(())
}

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}
