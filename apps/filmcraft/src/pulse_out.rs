//! Linux audio output selection. The primary backend speaks the PulseAudio native protocol
//! (PipeWire's `pipewire-pulse` or PulseAudio itself) through `filmcraft_pulse`,
//! pure Rust over a Unix socket; see that module for why cpal's ALSA path is not enough. cpal
//! remains the fallback when no sound-server socket is reachable, and can be forced by picking
//! "ALSA" as the Device Class in Settings ▸ Audio Hardware.
//!
//! A stream the sound server drops (a restarted `pipewire-pulse`) is reconnected by
//! [`AudioOut::recover`] with the same mixer and a continuous clock, like `CpalOut` restarts a
//! stream ALSA dropped. A stream the server refuses before it plays (no usable output), or one
//! it keeps dropping, falls back to cpal instead, until the audio settings change, so playback
//! is not left silent.
//!
//! Playback mixes for the output's own channel count, as on cpal: a 5.1 or 7.1 output gets a
//! 5.1 mix on its matching speakers, not a stereo downmix.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use filmcraft_engine::settings::AudioHardwarePrefs;
use filmcraft_pulse as pulse;
use filmcraft_ui_egui::{AudioDevices, AudioOut};

use crate::audio::CpalOut;

/// The host name this backend adds to Settings ▸ Audio Hardware ▸ Device Class.
const HOST_NAME: &str = "PipeWire / PulseAudio";
/// Device listings wait at most this long for the sound server, in total.
const LIST_TIMEOUT: Duration = Duration::from_secs(1);
/// A server that drops this many playing streams within [`DROP_WINDOW`] is given up (playback
/// moves to cpal).
const MAX_DROPS: usize = 3;
const DROP_WINDOW: Duration = Duration::from_secs(10);
/// After a drop, reconnects are tried this far apart for at most [`RECONNECT_FOR`]: long enough
/// for a server restart (`systemctl --user restart pulseaudio` takes about a second), after which
/// playback moves to cpal. A stream also waits this long for its first sound before the UI may
/// count the output as stalled.
const RECONNECT_INTERVAL: Duration = Duration::from_millis(250);
const RECONNECT_FOR: Duration = Duration::from_secs(3);

/// When a dropped stream is reconnected.
#[derive(Debug, Default)]
struct Reconnects {
    /// When playing streams were dropped, recently.
    drops: Vec<Instant>,
    /// Since when no stream has played (the first drop not yet reconnected).
    down_since: Option<Instant>,
    last_try: Option<Instant>,
}

/// What [`Reconnects::check`] decides about a stream that ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Reconnect {
    Now,
    Wait,
    GiveUp,
}

impl Reconnects {
    /// A stream ended; `was_playing` if the server had created it (a drop), otherwise it was a
    /// reconnect that failed (the server is still away).
    fn check(&mut self, now: Instant, was_playing: bool) -> Reconnect {
        let Some(since) = self.down_since.filter(|_| !was_playing) else {
            self.drops.retain(|t| now.saturating_duration_since(*t) < DROP_WINDOW);
            if self.drops.len() >= MAX_DROPS {
                return Reconnect::GiveUp;
            }
            self.drops.push(now);
            self.down_since = Some(now);
            self.last_try = Some(now);
            return Reconnect::Now;
        };
        if now.saturating_duration_since(since) >= RECONNECT_FOR {
            return Reconnect::GiveUp;
        }
        if self.last_try.is_some_and(|t| now.saturating_duration_since(t) < RECONNECT_INTERVAL) {
            return Reconnect::Wait;
        }
        self.last_try = Some(now);
        Reconnect::Now
    }

    /// A reconnected stream is playing.
    fn up(&mut self) {
        self.down_since = None;
    }
}

/// `AudioOut` over [`pulse::Playback`].
pub struct PulseOut {
    playback: Option<pulse::Playback>,
    /// Frames played by the streams before the current one (reconnects keep the clock going).
    base_frames: u64,
    reconnects: Reconnects,
    rate: u32,
    /// The channels the current stream was started with (what its mixer was built for).
    stream_channels: u8,
    /// The output's channel count as last seen, for the next stream.
    next_channels: u8,
    /// The current stream is the first since `start`, not a reconnect.
    first_stream: bool,
    /// The mixer of a stream given up after too many reconnects, and the frames heard by then,
    /// for the caller to play elsewhere.
    abandoned: Option<(pulse::Fill, u64)>,
    /// Since when nothing has been heard: a stream starting, or one dropped and reconnecting.
    waiting_since: Option<Instant>,
    hw: AudioHardwarePrefs,
    document_rate: Option<u32>,
    /// The outputs the last listing that answered found, shown when a later one times out.
    sinks: Mutex<Vec<pulse::Sink>>,
}

impl PulseOut {
    pub fn new() -> Self {
        Self {
            playback: None,
            base_frames: 0,
            reconnects: Reconnects::default(),
            rate: 48_000,
            stream_channels: 2,
            next_channels: 2,
            first_stream: false,
            abandoned: None,
            waiting_since: None,
            hw: AudioHardwarePrefs::default(),
            document_rate: None,
            sinks: Mutex::new(Vec::new()),
        }
    }

    /// The rate playback mixes at: the settings' rate, or the sequence's when forced. The
    /// server resamples, so the asked-for rate is always granted.
    fn desired_rate(&self) -> u32 {
        let want = if self.hw.force_document_rate { self.document_rate.unwrap_or(self.hw.sample_rate) } else { self.hw.sample_rate };
        if want == 0 { 48_000 } else { want }
    }

    /// The chosen output as Settings stores it (its description), looked up by the stream's own
    /// worker; `None` follows the default sink.
    fn target(&self) -> Option<String> {
        (!self.hw.default_output.is_empty()).then(|| self.hw.default_output.clone())
    }

    /// Keep the output's channel count a stream has found, for the next stream.
    fn note_channels(&mut self, playback: &pulse::Playback) {
        if let Some(c) = playback.sink_channels() {
            self.next_channels = c.clamp(1, MAX_CHANNELS);
        }
    }

    /// A mixer for the caller to play elsewhere, with the clock position it goes on from (see
    /// [`mixer_position`]): that of a first
    /// stream the server ended before creating it (it refused the stream, or the output was
    /// gone), or of a stream [`AudioOut::recover`] gave up reconnecting.
    fn take_for_fallback(&mut self) -> Option<(pulse::Fill, u64)> {
        if let Some(abandoned) = self.abandoned.take() {
            return Some(abandoned);
        }
        if !self.first_stream || !self.playback.as_ref().is_some_and(|p| p.ended() && !p.created()) {
            return None;
        }
        let old = self.playback.take()?;
        self.note_channels(&old);
        old.into_fill().map(|fill| (fill, 0))
    }
}

/// Where the clock of the next output starts when it takes over the mixer from an ended stream:
/// the frames the mixer has produced, `base` from earlier streams plus those sent on this one. The
/// mixer goes on from there, so the frames still buffered in the server when it went are skipped,
/// not waited for; starting from the frames heard instead left the clock behind the sound by them.
fn mixer_position(base: u64, sent: u64) -> u64 {
    base.saturating_add(sent)
}

/// The most channels a stream asks for (the protocol's limit).
const MAX_CHANNELS: u8 = 32;

impl AudioOut for PulseOut {
    fn start(&mut self, fill: Box<dyn FnMut(&mut [f32], usize) + Send>) -> Result<u32, String> {
        self.stop();
        self.rate = self.desired_rate();
        self.base_frames = 0;
        self.reconnects = Reconnects::default();
        self.abandoned = None;
        self.stream_channels = self.next_channels;
        let playback = pulse::Playback::start(self.rate, self.stream_channels, self.target(), self.hw.buffer_size.max(64), fill)?;
        self.playback = Some(playback);
        self.first_stream = true;
        self.waiting_since = Some(Instant::now());
        Ok(self.rate)
    }
    fn stop(&mut self) {
        if let Some(old) = self.playback.take() {
            self.note_channels(&old);
        }
    }
    fn recover(&mut self) {
        let Some(current) = self.playback.as_ref() else { return };
        if !current.ended() {
            if current.heard_frames() > 0 {
                self.reconnects.up();
                self.waiting_since = None;
            }
            return;
        }
        let was_playing = current.created();
        if was_playing {
            // nothing is heard from the drop until a reconnected stream plays
            self.waiting_since = Some(Instant::now());
        }
        match self.reconnects.check(Instant::now(), was_playing) {
            // Keep the ended stream (and its frozen clock) until the next attempt.
            Reconnect::Wait => return,
            Reconnect::GiveUp => {
                log::warn!("the sound server is gone or keeps dropping the audio stream");
                self.waiting_since = None;
                if let Some(old) = self.playback.take() {
                    self.note_channels(&old);
                    let at = mixer_position(self.base_frames, old.frames_sent());
                    self.abandoned = old.into_fill().map(|fill| (fill, at));
                }
                return;
            }
            Reconnect::Now => {}
        }
        let Some(old) = self.playback.take() else { return };
        self.note_channels(&old);
        self.first_stream = false;
        self.base_frames = mixer_position(self.base_frames, old.frames_sent());
        let Some(fill) = old.into_fill() else { return };
        if was_playing {
            log::warn!("the sound server dropped the audio stream; reconnecting");
        }
        // The same channel count: the mixer was built for it.
        match pulse::Playback::start(self.rate, self.stream_channels, self.target(), self.hw.buffer_size.max(64), fill) {
            Ok(p) => {
                self.playback = Some(p);
                // each stream gets its own time to start (the drop's retries are bounded apart)
                self.waiting_since = Some(Instant::now());
            }
            Err(e) => log::warn!("the audio stream could not be reconnected: {e}"),
        }
    }
    fn sample_rate(&self) -> u32 {
        self.rate
    }
    fn channels(&self) -> usize {
        // Read before `start` to build the mixer: the output's count then, the stream's after.
        usize::from(if self.playback.is_some() { self.stream_channels } else { self.next_channels })
    }
    fn waiting(&self) -> bool {
        self.playback.is_some() && self.waiting_since.is_some_and(|t| t.elapsed() < RECONNECT_FOR)
    }
    fn clock_is_heard(&self) -> bool {
        true
    }
    fn played_frames(&self) -> Option<u64> {
        // Frames heard, not frames sent, so the picture follows what is audible. An ended stream
        // keeps its last reading until `recover` reconnects or gives up.
        self.playback.as_ref().and_then(|p| p.clock_frames().or(Some(p.heard_frames()))).map(|f| self.base_frames.saturating_add(f))
    }
    fn devices(&self) -> AudioDevices {
        let mut known = self.sinks.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        match pulse::list_sinks(LIST_TIMEOUT) {
            Ok(sinks) => *known = sinks,
            Err(e) => log::info!("audio outputs: {e}; showing the last known list"),
        }
        let outputs = known.iter().map(|s| s.description.clone()).collect();
        let chosen = self.target().and_then(|t| known.iter().find(|s| s.name == t || s.description == t).map(|s| s.channels));
        let output_channels = u16::from(chosen.unwrap_or(self.next_channels));
        AudioDevices { hosts: vec![HOST_NAME.into()], inputs: Vec::new(), outputs, output_channels }
    }
    fn configure(&mut self, hw: &AudioHardwarePrefs, document_rate: Option<u32>) {
        self.hw = hw.clone();
        self.document_rate = document_rate;
        self.rate = self.desired_rate();
        // The mixer is built for the output's channel count before `start`, so it is asked here,
        // like cpal's device configuration; a stream also reports it for the next one.
        match pulse::output_channels(self.target(), LIST_TIMEOUT) {
            Ok(c) => self.next_channels = c.clamp(1, MAX_CHANNELS),
            Err(e) => log::debug!("audio output channels: {e}"),
        }
    }
}

/// The desktop audio output: the sound-server backend when its socket is reachable and chosen
/// (or by default), cpal otherwise.
pub struct SystemOut {
    pulse: PulseOut,
    cpal: CpalOut,
    hw: AudioHardwarePrefs,
    pulse_available: bool,
    /// Which backend `start` last used, so the clock and device queries stay consistent.
    active_pulse: bool,
    /// The sound server refused a stream or kept dropping it: use cpal until the settings change.
    pulse_refused: bool,
    /// Frames heard on the sound server before playback moved to cpal, so the clock goes on.
    cpal_base: u64,
}

impl SystemOut {
    pub fn new() -> Self {
        let pulse_available = pulse::available();
        Self {
            pulse: PulseOut::new(),
            cpal: CpalOut::new(),
            hw: AudioHardwarePrefs::default(),
            pulse_available,
            active_pulse: pulse_available,
            pulse_refused: false,
            cpal_base: 0,
        }
    }

    fn use_pulse(&self) -> bool {
        // "PipeWire" and "PulseAudio" are accepted as synonyms, so a Device Class saved under
        // an earlier or shorter name keeps selecting this backend.
        self.pulse_available && !self.pulse_refused && matches!(self.hw.device_class.as_str(), "" | HOST_NAME | "PipeWire" | "PulseAudio")
    }

    fn active(&self) -> &dyn AudioOut {
        if self.active_pulse { &self.pulse } else { &self.cpal }
    }
}

impl AudioOut for SystemOut {
    fn start(&mut self, fill: Box<dyn FnMut(&mut [f32], usize) + Send>) -> Result<u32, String> {
        self.pulse.stop();
        self.cpal.stop();
        // Checked at every start: the sound server may have stopped (or started) since launch,
        // and a stale socket file must fall back to cpal instead of playing nothing.
        self.pulse_available = pulse::available();
        self.active_pulse = self.use_pulse();
        self.cpal_base = 0;
        if self.active_pulse { self.pulse.start(fill) } else { self.cpal.start(fill) }
    }
    fn stop(&mut self) {
        self.pulse.stop();
        self.cpal.stop();
    }
    fn sample_rate(&self) -> u32 {
        self.active().sample_rate()
    }
    fn channels(&self) -> usize {
        self.active().channels()
    }
    fn played_frames(&self) -> Option<u64> {
        if self.active_pulse { self.pulse.played_frames() } else { self.cpal.played_frames().map(|f| self.cpal_base.saturating_add(f)) }
    }
    fn recover(&mut self) {
        if !self.active_pulse {
            return self.cpal.recover();
        }
        let fallback = match self.pulse.take_for_fallback() {
            Some(f) => Some(f),
            None => {
                self.pulse.recover();
                self.pulse.take_for_fallback()
            }
        };
        let Some((fill, heard)) = fallback else { return };
        // cpal takes over with the same mixer, and the clock goes on from what was heard, if it
        // runs at the rate the mixer was built for; otherwise the next playback starts on cpal.
        self.pulse_refused = true;
        self.active_pulse = false;
        self.cpal_base = heard;
        if self.cpal.sample_rate() != self.pulse.sample_rate() {
            log::warn!("the sound server failed the audio stream; the next playback uses ALSA");
            return;
        }
        log::warn!("the sound server failed the audio stream; playing through ALSA");
        if let Err(e) = self.cpal.start(fill) {
            log::warn!("ALSA output: {e}");
        }
    }
    fn note(&self) -> Option<String> {
        self.active().note()
    }
    fn waiting(&self) -> bool {
        self.active_pulse && self.pulse.waiting()
    }
    fn clock_is_heard(&self) -> bool {
        self.active_pulse
    }
    fn devices(&self) -> AudioDevices {
        let cpal = self.cpal.devices();
        let mut hosts = Vec::new();
        if self.pulse_available {
            hosts.push(HOST_NAME.to_string());
        }
        hosts.extend(cpal.hosts.iter().cloned());
        let mut devices = if self.use_pulse() { self.pulse.devices() } else { cpal };
        devices.hosts = hosts;
        devices
    }
    fn configure(&mut self, hw: &AudioHardwarePrefs, document_rate: Option<u32>) {
        self.hw = hw.clone();
        self.pulse_refused = false;
        self.pulse.configure(hw, document_rate);
        self.cpal.configure(hw, document_rate);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ms(n: u64) -> Duration {
        Duration::from_millis(n)
    }

    #[test]
    fn a_dropped_stream_is_reconnected_through_a_server_restart_then_given_up() {
        // Before, three tries 250 ms apart gave up after 0.75 s, sooner than PulseAudio restarts.
        let t0 = Instant::now();
        let mut r = Reconnects::default();
        assert_eq!(r.check(t0, true), Reconnect::Now, "a drop is reconnected at once");
        assert_eq!(r.check(t0 + ms(100), false), Reconnect::Wait, "the server gets a moment between attempts");
        for t in (250..3000).step_by(250) {
            assert_eq!(r.check(t0 + ms(t), false), Reconnect::Now, "still trying at {t} ms");
        }
        assert_eq!(r.check(t0 + ms(3000), false), Reconnect::GiveUp, "a server away for 3 s is given up");
    }

    #[test]
    fn the_next_output_starts_its_clock_where_the_mixer_is() {
        // 48 000 frames from earlier streams; this one sent 9 600 of which 4 800 were heard when
        // the server went. The mixer produces frame 57 600 next, so that is where the clock goes on.
        assert_eq!(mixer_position(48_000, 9_600), 57_600);
        assert_eq!(mixer_position(u64::MAX, 1), u64::MAX, "saturates");
    }

    #[test]
    fn a_server_that_keeps_dropping_playing_streams_is_given_up() {
        let t0 = Instant::now();
        let mut r = Reconnects::default();
        for t in [0, 1000, 2000] {
            assert_eq!(r.check(t0 + ms(t), true), Reconnect::Now);
            r.up();
        }
        assert_eq!(r.check(t0 + ms(3000), true), Reconnect::GiveUp, "the fourth drop within 10 s");
        // a server that drops the stream once in a while is always reconnected
        let mut r = Reconnects::default();
        for t in [0, 11_000, 22_000, 33_000] {
            assert_eq!(r.check(t0 + ms(t), true), Reconnect::Now);
            r.up();
        }
    }

    #[test]
    fn the_cpal_fallback_keeps_its_recovery_and_status_note() {
        // Without these forwarded, the ALSA path lost CpalOut's stream restarts (#840) and its
        // "default device cannot be opened" note once wrapped in SystemOut.
        let cpal = CpalOut::new();
        let expected = cpal.note();
        let mut out = SystemOut {
            pulse: PulseOut::new(),
            cpal,
            hw: AudioHardwarePrefs::default(),
            pulse_available: false,
            active_pulse: false,
            pulse_refused: false,
            cpal_base: 0,
        };
        assert_eq!(out.note(), expected);
        out.recover();
        assert_eq!(out.played_frames(), None, "nothing is playing");
    }

    #[test]
    fn the_device_class_picks_the_backend() {
        let picks_pulse = |class: &str, socket: bool| {
            let hw = AudioHardwarePrefs { device_class: class.into(), ..Default::default() };
            SystemOut {
                pulse: PulseOut::new(),
                cpal: CpalOut::without_device(),
                hw,
                pulse_available: socket,
                active_pulse: false,
                pulse_refused: false,
                cpal_base: 0,
            }
            .use_pulse()
        };
        for class in ["", HOST_NAME, "PipeWire", "PulseAudio"] {
            assert!(picks_pulse(class, true), "{class:?} plays through the sound server");
            assert!(!picks_pulse(class, false), "{class:?} without a reachable socket falls back to cpal");
        }
        assert!(!picks_pulse("ALSA", true), "ALSA is cpal, even with a sound server running");
        let refused = SystemOut {
            pulse: PulseOut::new(),
            cpal: CpalOut::without_device(),
            hw: AudioHardwarePrefs::default(),
            pulse_available: true,
            active_pulse: false,
            pulse_refused: true,
            cpal_base: 0,
        };
        assert!(!refused.use_pulse(), "after the server refused a stream, cpal until the settings change");
    }

    #[test]
    fn an_idle_pulse_output_has_no_clock_and_recover_is_a_no_op() {
        let mut out = PulseOut::new();
        out.recover();
        assert_eq!(out.played_frames(), None);
    }
}
