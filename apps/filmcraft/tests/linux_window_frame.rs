//! Issue #433: the workspace builds eframe/winit with `default-features = false`, which silently
//! dropped winit's Adwaita client-side frame, so GNOME Wayland got winit's plain fallback bar.
//! The Linux build turns it back on with `wayland-csd-adwaita-notitle` (vector controls, no title
//! font). Compositors that grant server-side decorations (Hyprland, KDE, sway) never draw it.
//! These checks read the manifest and the lock file, so they run (and catch a regression) on
//! every platform's CI, not only on Linux.

const MANIFEST: &str = include_str!("../Cargo.toml");
const LOCK: &str = include_str!("../../../Cargo.lock");

/// The `[[package]]` block of `name` in Cargo.lock (first version listed).
fn lock_package(name: &str) -> Option<&'static str> {
    LOCK.split("[[package]]").find(|b| b.lines().any(|l| l.trim() == format!("name = \"{name}\"")))
}

#[test]
fn linux_winit_enables_the_adwaita_frame_without_a_title_font() {
    let linux = MANIFEST
        .split("\n[")
        .find(|s| s.starts_with("target.'cfg(target_os = \"linux\")'.dependencies]"))
        .expect("apps/filmcraft/Cargo.toml has a Linux dependency table");
    let winit = linux.lines().find(|l| l.trim_start().starts_with("winit")).expect("the Linux table lists winit");
    assert!(winit.contains("\"wayland-csd-adwaita-notitle\""), "Linux winit must enable the Adwaita frame: {winit}");
    // The plain `wayland-csd-adwaita` feature pulls in a title font renderer (and Cantarell).
    assert!(!winit.contains("\"wayland-csd-adwaita\""), "use the -notitle variant, no bundled font: {winit}");
}

#[test]
fn lock_resolves_sctk_adwaita_for_winit_without_font_crates() {
    let winit = lock_package("winit").expect("winit is in Cargo.lock");
    assert!(winit.contains("\"sctk-adwaita\""), "winit must depend on sctk-adwaita (the Adwaita frame)");
    let adwaita = lock_package("sctk-adwaita").expect("sctk-adwaita is in Cargo.lock");
    for font_crate in ["ab_glyph", "crossfont", "memmap2"] {
        assert!(!adwaita.contains(&format!("\"{font_crate}")), "sctk-adwaita must not pull {font_crate} (title font)");
    }
}
