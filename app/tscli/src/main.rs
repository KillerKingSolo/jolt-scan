mod points;
mod process;
mod rand;
use rand::JavaRandom;
use std::io::{BufWriter, Read, Write};
use std::process::{Command, Stdio};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Offset { Up, Down, Neutral }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Jolt { pub food_amount: i32, pub pattern: [Offset; 10] }

fn off_val(o: Offset) -> i32 { match o { Offset::Up => -1, Offset::Neutral => 0, Offset::Down => 1 } }

// skip: heart rolls before the hunger icons; 0 normally, one per drawn heart at low health
fn check_tick(tick: i32, pattern: &[i32; 10], skip: usize) -> bool {
    let mut r = JavaRandom::with_seed(tick.wrapping_mul(312871) as u64);
    if skip > 0 { r.skip_n(skip as u64); }
    for &e in pattern.iter().rev() { if r.next_int_n(3) - 1 != e { return false; } }
    true
}

fn crack(j: &Jolt, skip: usize, max_tick: i32, n: usize) -> Vec<i32> {
    let p = j.pattern.map(off_val);
    let step = j.food_amount * 3 + 1;
    if step <= 0 { return vec![]; }
    (0..=max_tick).step_by(step as usize).filter(|&t| check_tick(t, &p, skip)).take(n).collect()
}

// low health rolls once per heart drawn: 10 for 20 max health, more with absorption or extra max health.
// The neighbour check downstream keeps whichever count lines up.
const LH_SKIPS: std::ops::RangeInclusive<usize> = 10..=14;

fn check_jolt(j: &Jolt) -> bool {
    if j.pattern.iter().all(|f| *f == Offset::Neutral) { return false; }
    let c = |o| j.pattern.iter().filter(|&&x| x == o).count();
    c(Offset::Up).max(c(Offset::Neutral)).max(c(Offset::Down)) <= 7
}

fn pat_str(j: &Jolt) -> String {
    j.pattern.iter().map(|o| match o { Offset::Up => '+', Offset::Down => '-', Offset::Neutral => '0' }).collect()
}

fn load_foods(w: u32) -> [image::RgbaImage; 3] {
    let scale = w as f64 / 81.0;
    let fw = (9.0 * scale).round() as u32;
    let raw = [include_bytes!("../assets/food_empty.png").as_slice(), include_bytes!("../assets/food_half.png"), include_bytes!("../assets/food.png")];
    std::array::from_fn(|i| image::load_from_memory(raw[i]).unwrap().resize_exact(fw, fw, image::imageops::FilterType::Nearest).to_rgba8())
}

// half hearts in the bottom heart row: (red, coloured). Red is normal health; coloured also counts
// poison, frozen and absorption hearts, whose exact colours we have no footage of, so any strong colour.
// Empty hearts are neutral gray, normal (46,48,45) or dimmed by a menu (25,22,23). bbox = resting health bar.
fn read_hearts(img: &image::RgbImage, hx: u32, hy: u32, hw: u32) -> (u32, u32) {
    let s = hw as f64 / 81.0;
    let half = |x0: f64, x1: f64| {
        // GUI rows 2..7 stay inside the heart when it shakes down 1 px
        let (y0, y1) = (hy + (2.0 * s) as u32, hy + (7.0 * s) as u32);
        let (mut red, mut col, mut all) = (0, 0, 0);
        for y in y0..y1 { for x in x0 as u32..x1 as u32 {
            let p = img.get_pixel(x, y).0.map(|v| v as u32);
            all += 1;
            // floor 45: menus dim hearts to about (70,20,20)
            if p[0] > 45 && p[1] * 2 < p[0] && p[2] * 2 < p[0] { red += 1; }
            let (hi, lo) = (p[0].max(p[1]).max(p[2]), p[0].min(p[1]).min(p[2]));
            if hi > 60 && hi - lo > 40 { col += 1; }
        } }
        ((red * 2 > all) as u32, (col * 2 > all) as u32)
    };
    (0..10).fold((0, 0), |(r, c), i| {
        let l = hx as f64 + i as f64 * 8.0 * s;
        let (a, b) = (half(l + 2.0 * s, l + 4.0 * s), half(l + 5.0 * s, l + 7.0 * s));
        (r + a.0 + b.0, c + a.1 + b.1)
    })
}

// food icons as 9x9 GUI-pixel templates; None = transparent
fn food_templates() -> Vec<[Option<[f64; 3]>; 81]> {
    load_foods(81).iter().map(|img| std::array::from_fn(|k| {
        let p = img.get_pixel((k % 9) as u32, (k / 9) as u32).0;
        (p[3] >= 128).then(|| [p[0] as f64, p[1] as f64, p[2] as f64])
    })).collect()
}

// mean channel distance between the resting hunger bar and the food icons, 0..255, lower is better.
// f is an rgb24 crop of fw x fh whose top-left sits at (x0, y0) in the video frame.
#[allow(clippy::too_many_arguments)]
fn bar_score(f: &[u8], fw: u32, fh: u32, x0: u32, y0: u32, tpl: &[[Option<[f64; 3]>; 81]], s: f64, cx: f64, by: f64) -> f64 {
    let px = |x: f64, y: f64| -> Option<[f64; 3]> {
        let (xi, yi) = (x as i64 - x0 as i64, y as i64 - y0 as i64);
        if xi < 0 || yi < 0 || xi >= fw as i64 || yi >= fh as i64 { return None; }
        let o = ((yi as u32 * fw + xi as u32) * 3) as usize;
        Some([f[o] as f64, f[o + 1] as f64, f[o + 2] as f64])
    };
    let hx = cx + 10.0 * s;
    let mut bright: f64 = 1.0;
    for gy in 0..9 { for gx in 0..81 {
        if let Some(p) = px(hx + (gx as f64 + 0.5) * s, by + (gy as f64 + 0.5) * s) { bright = bright.max(p[0].max(p[1]).max(p[2])); }
    } }
    let gain = 255.0 / bright;
    let mut total = 0.0;
    for i in 0..10 {
        let ix = hx + i as f64 * 8.0 * s;
        let mut best = f64::MAX;
        // resting position only: allowing a jolt offset here makes the bar height ambiguous by 1 GUI px
        for t in tpl {
            let (mut sum, mut cnt) = (0.0, 0.0);
            for (k, c) in t.iter().enumerate() {
                let Some(c) = c else { continue };
                let Some(p) = px(ix + ((k % 9) as f64 + 0.5) * s, by + ((k / 9) as f64 + 0.5) * s) else { return f64::MAX };
                sum += (p[0] * gain - c[0]).abs() + (p[1] * gain - c[1]).abs() + (p[2] * gain - c[2]).abs();
                cnt += 3.0;
            }
            best = best.min(sum / cnt);
        }
        total += best;
    }
    total / 10.0
}

// usage: tscli hudcheck <video> <width> <height> <s> <cx> <by>
// scores the HUD position on every keyframe and prints "seconds,score"; other POVs, another GUI scale
// or a hidden HUD score high. Keyframes only, so even a long 4K video takes a minute or two.
fn hudcheck(a: &[String]) {
    let (w, h): (u32, u32) = (a[3].parse().unwrap(), a[4].parse().unwrap());
    let (s, cx, by): (f64, f64, f64) = (a[5].parse().unwrap(), a[6].parse().unwrap(), a[7].parse().unwrap());
    let x0 = (cx - 95.0 * s).max(0.0) as u32;
    let y0 = (by - 3.0 * s).max(0.0) as u32;
    let fw = ((cx + 95.0 * s) as u32).min(w) - x0;
    let fh = ((by + 12.0 * s) as u32).min(h) - y0;
    let mut child = Command::new("ffmpeg").args(["-hide_banner", "-nostats", "-v", "info", "-skip_frame", "nokey", "-i", &a[2],
        "-vf", &format!("crop={fw}:{fh}:{x0}:{y0}:exact=1,showinfo"), "-fps_mode", "passthrough", "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        .stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().expect("ffmpeg");
    // showinfo prints each frame's time on stderr, in the same order as the frames on stdout
    let err = child.stderr.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        use std::io::BufRead;
        for line in std::io::BufReader::new(err).lines().map_while(Result::ok) {
            if let Some(i) = line.find("pts_time:") {
                let t: f64 = line[i + 9..].split_whitespace().next().and_then(|v| v.parse().ok()).unwrap_or(f64::NAN);
                if tx.send(t).is_err() { break; }
            }
        }
    });
    let tpl = food_templates();
    let mut out = child.stdout.take().unwrap();
    let mut buf = vec![0u8; (fw * fh * 3) as usize];
    let mut stdout = BufWriter::new(std::io::stdout());
    while out.read_exact(&mut buf).is_ok() {
        let t = rx.recv().unwrap_or(f64::NAN);
        writeln!(stdout, "{t:.3},{:.2}", bar_score(&buf, fw, fh, x0, y0, &tpl, s, cx, by)).unwrap();
    }
    stdout.flush().unwrap();
    let _ = child.wait();
}

// usage: ffmpeg ... 22x21 rgb24 frames of the menu panel area | tscli panel
// prints, per frame, the share of blocks that are the light neutral gray of an open container panel
fn panel() {
    let size = 22 * 21 * 3;
    let mut buf = vec![0u8; size];
    let mut input = std::io::stdin().lock();
    let mut stdout = BufWriter::new(std::io::stdout());
    while input.read_exact(&mut buf).is_ok() {
        let hit = buf.chunks_exact(3).filter(|p| (120..=215).contains(&p[0]) && p[0].abs_diff(p[1]) < 10 && p[0].abs_diff(p[2]) < 10).count();
        writeln!(stdout, "{:.2}", hit as f64 / 462.0).unwrap();
    }
    stdout.flush().unwrap();
}

// usage: tscli hudfind <video> <width> <height> <duration_s> [samples]
// finds GUI scale s (video px per GUI px), hotbar center x and bar top y from the hunger icons
fn hudfind(a: &[String]) {
    let (w, h): (u32, u32) = (a[3].parse().unwrap(), a[4].parse().unwrap());
    let dur: f64 = a[5].parse().unwrap();
    let n: usize = a.get(6).map(|s| s.parse().unwrap()).unwrap_or(16);
    let y0 = h / 2;
    let ch = h - y0;
    let frames: Vec<Vec<u8>> = (0..n).filter_map(|i| {
        let t = dur * (0.04 + 0.92 * i as f64 / (n.max(2) - 1) as f64);
        let o = Command::new("ffmpeg").args(["-v", "error", "-ss", &format!("{t:.2}"), "-i", &a[2], "-frames:v", "1",
            "-vf", &format!("crop={w}:{ch}:0:{y0},format=rgb24"), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]).output().ok()?;
        (o.stdout.len() == (w * ch * 3) as usize).then_some(o.stdout)
    }).collect();
    let tpl = food_templates();
    // median over frames, so frames without a HUD do not decide
    let score = |s: f64, cx: f64, by: f64| -> f64 {
        let mut v: Vec<f64> = frames.iter().map(|f| bar_score(f, w, ch, 0, y0, &tpl, s, cx, by)).collect();
        v.sort_by(|a, b| a.partial_cmp(b).unwrap());
        v.get(v.len() / 2).copied().unwrap_or(f64::MAX)
    };
    let smax = (w as f64 / 182.0).min(h as f64 / 40.0);
    let mut coarse: Vec<(f64, f64)> = (0..).map(|k| 1.0 + k as f64 * 0.05).take_while(|&s| s <= smax)
        .map(|s| (score(s, w as f64 / 2.0, h as f64 - 39.0 * s), s)).collect();
    coarse.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
    let mut best = (f64::MAX, 0.0, 0.0, 0.0);
    for &(_, s0) in coarse.iter().take(5) {
        for ks in -5..=5 { let s = s0 + ks as f64 * 0.01;
            for kx in -6..=6 { let cx = w as f64 / 2.0 + kx as f64 * 0.5;
                for ky in -6..=6 { let by = h as f64 - 39.0 * s + ky as f64 * 0.5;
                    let v = score(s, cx, by);
                    if v < best.0 { best = (v, s, cx, by); }
                } } }
    }
    let median = coarse.get(coarse.len() / 2).map_or(0.0, |c| c.0);
    println!("{{\"s\":{:.3},\"cx\":{:.1},\"by\":{:.1},\"score\":{:.2},\"typical\":{:.2},\"frames\":{}}}", best.1, best.2, best.3, best.0, median, frames.len());
}

// usage: tscli selftest — checks the cracker and the icon matcher, exit code 1 on failure (used before installing an update)
fn selftest() -> bool {
    let jolt = |food: i32, p: &str| Jolt { food_amount: food, pattern: std::array::from_fn(|i| match p.as_bytes()[i] { b'+' => Offset::Up, b'-' => Offset::Down, _ => Offset::Neutral }) };
    let crack_ok = crack(&jolt(20, "00--+00+0-"), 0, 600000, 1) == [23546] && crack(&jolt(6, "+--++0-++0"), 10, 600000, 1) == [47918];
    // nextInt(3) must reroll a draw of 2^31-1 like Java does
    let mut r = JavaRandom::with_raw_seed(0x7FFF_FFFFu64 << 17);
    r.previous();
    let mut after = JavaRandom { state: r.state };
    after.advance();
    let crack_ok = crack_ok && r.next_int_n(3) == after.next_bits(31) % 3;
    // a synthetic full hunger bar at 4 px per GUI pixel with a known jolt
    let (s, w) = (4u32, 324u32);
    let foods = load_foods(w);
    let fw = foods[2].width();
    let pad = ((9 * s) as f32 / 9.0).round() as u32;
    let h = 9 * s + 2 * pad;
    let want = jolt(20, "+-0+-0+-0+");
    let mut img = image::RgbImage::from_pixel(w, h, image::Rgb([60, 70, 60]));
    for (i, o) in want.pattern.iter().enumerate() {
        let y0 = match o { Offset::Up => 0, Offset::Neutral => s, Offset::Down => 2 * s };
        for (x, y, p) in foods[2].enumerate_pixels() {
            let (a, d) = (p.0[3] as u32, img.get_pixel_mut(i as u32 * 8 * s + x, y0 + y));
            for c in 0..3 { d.0[c] = ((p.0[c] as u32 * a + d.0[c] as u32 * (255 - a)) / 255) as u8; }
        }
    }
    let got = process::Matcher::new(&foods).match_img(&img, &points::prepare_points(s as f64, w, h, fw, fw));
    let match_ok = got.as_ref().is_ok_and(|j| *j == want);
    println!("crack {} matcher {} ({:?})", if crack_ok { "ok" } else { "FAILED" }, if match_ok { "ok" } else { "FAILED" }, got.map(|j| pat_str(&j)));
    crack_ok && match_ok
}

// usage: tscli <video> <x> <y> <w> <h> <fps> [max_tick] [hx hy hw hh]
// env TSCLI_SS / TSCLI_T: seek and duration in seconds; TSCLI_PROGRESS: print "p <frame>" to stderr
fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.get(1).map(String::as_str) == Some("hudfind") { return hudfind(&a); }
    if a.get(1).map(String::as_str) == Some("selftest") { std::process::exit(if selftest() { 0 } else { 1 }); }
    if a.get(1).map(String::as_str) == Some("hudcheck") { return hudcheck(&a); }
    if a.get(1).map(String::as_str) == Some("panel") { return panel(); }
    let num = |i: usize| -> u32 { a[i].parse().unwrap() };
    let (x, y, w, h0) = (num(2), num(3), num(4), num(5));
    let fps: f64 = a[6].parse().unwrap();
    let max_tick: i32 = a.get(7).map(|s| s.parse().unwrap()).unwrap_or(20 * 3600 * 12);
    let hbox = (a.len() >= 12).then(|| (num(8), num(9), num(10), num(11)));
    // same bbox expansion as tick_studio when not jolting
    let pad = (h0 as f32 / 9.0).round() as u32;
    let (y, h) = (y.saturating_sub(pad), h0 + 2 * pad);
    // one ffmpeg crop covering hunger box and padded health box
    let (cx, cy, cx1, cy1) = match hbox {
        Some((hx, hy, hw, hh)) => (x.min(hx), y.min(hy.saturating_sub(pad)), (x + w).max(hx + hw), (y + h).max(hy + hh + pad)),
        None => (x, y, x + w, y + h),
    };
    let (cw, ch) = (cx1 - cx, cy1 - cy);
    let scale = w as f64 / 81.0;
    let fw = (9.0 * scale).round() as u32;
    let matcher = process::Matcher::new(&load_foods(w));
    let pts = points::prepare_points(scale, w, h, fw, fw);
    let mut cmd = Command::new("ffmpeg");
    cmd.args(["-v", "error"]);
    if let Ok(ss) = std::env::var("TSCLI_SS") { cmd.args(["-ss", &ss]); }
    if let Ok(t) = std::env::var("TSCLI_T") { cmd.args(["-t", &t]); }
    let mut child = cmd.args(["-i", &a[1], "-vf", &format!("crop={cw}:{ch}:{cx}:{cy}:exact=1,format=rgb24"), "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]).stdout(Stdio::piped()).spawn().expect("ffmpeg");
    let progress = std::env::var("TSCLI_PROGRESS").is_ok();
    let mut out = child.stdout.take().unwrap();
    let mut buf = vec![0u8; (cw * ch * 3) as usize];
    let mut stdout = BufWriter::new(std::io::stdout());
    writeln!(stdout, "frame,video_tick,food,pattern,hp,game_ticks,low_health_ticks").unwrap();
    let hp_debug = std::env::var("TSCLI_HP_DEBUG").is_ok();
    let mut last: Option<Jolt> = None;
    let mut n: i64 = -1;
    while out.read_exact(&mut buf).is_ok() {
        n += 1;
        if progress && n % 300 == 0 { eprintln!("p {n}"); }
        let full = image::RgbImage::from_raw(cw, ch, buf.clone()).unwrap();
        let (red, col) = hbox.map_or((0, 0), |(hx, hy, hw, _)| read_hearts(&full, hx - cx, hy - cy, hw));
        if hp_debug { eprintln!("{n},{},{col}", if red > 0 { red.to_string() } else { "?".into() }); }
        // no coloured hearts where the health box says: other GUI layout, HUD hidden or wither, so jolts are junk
        if hbox.is_some() && col == 0 { continue; }
        let img = image::imageops::crop_imm(&full, x - cx, y - cy, w, h).to_image();
        let Some(j) = matcher.match_img(&img, &pts).ok().filter(check_jolt) else { continue };
        if last == Some(j) { continue; }
        last = Some(j);
        let vt = ((n as f64 / fps) * 20.0).round() as i32;
        // Some(true): low health only, Some(false): normal only, None: unsure, so both.
        // Coloured halves beyond the red ones are absorption or effect hearts, which change what counts as low.
        let low = if hbox.is_none() || red == 0 || col > red { None } else { Some(red <= 4) };
        let join = |v: Vec<i32>| v.iter().map(|t| t.to_string()).collect::<Vec<_>>().join(" ");
        let normal = if low == Some(true) { String::new() } else { join(crack(&j, 0, max_tick, 3)) };
        let lowh = if low == Some(false) { String::new() } else {
            let mut v: Vec<i32> = LH_SKIPS.flat_map(|k| crack(&j, k, max_tick, 2)).collect();
            v.sort_unstable(); v.dedup(); join(v)
        };
        writeln!(stdout, "{n},{vt},{},{},{},{normal},{lowh}", j.food_amount, pat_str(&j), if red > 0 { red.to_string() } else { String::new() }).unwrap();
        stdout.flush().unwrap();
    }
    let _ = child.wait();
}
