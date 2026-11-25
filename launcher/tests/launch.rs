use std::process::Command;
use std::time::Duration;

use minecrafttools_launcher::launch::{Handshake, LaunchError};

fn subject() -> Command {
    Command::new(env!("CARGO_BIN_EXE_demo-subject"))
}

#[test]
fn demo_subject_reports_ready() {
    let handshake = Handshake::bind().unwrap();
    let mut child = subject()
        .env("MINECRAFTTOOLS_LAUNCHER_PORT", handshake.port().to_string())
        .env("MINECRAFTTOOLS_LAUNCHER_TOKEN", handshake.token())
        .env("DEMO_SUBJECT_DELAY_MS", "40")
        .env("DEMO_SUBJECT_LIFETIME_MS", "50")
        .spawn()
        .unwrap();

    handshake
        .wait_for_ready(&mut child, Duration::from_secs(5))
        .unwrap();
    let _ = child.kill();
    let _ = child.wait();
}

#[test]
fn early_exit_is_reported() {
    let handshake = Handshake::bind().unwrap();
    let mut child = subject()
        .env("MINECRAFTTOOLS_LAUNCHER_PORT", handshake.port().to_string())
        .env("MINECRAFTTOOLS_LAUNCHER_TOKEN", handshake.token())
        .env("DEMO_SUBJECT_MODE", "exit")
        .spawn()
        .unwrap();

    match handshake.wait_for_ready(&mut child, Duration::from_secs(2)) {
        Err(LaunchError::EarlyExit(code)) => assert_eq!(code, Some(1)),
        other => panic!("expected early exit, got {other:?}"),
    }
    let _ = child.wait();
}

#[test]
fn missing_ready_times_out() {
    let handshake = Handshake::bind().unwrap();
    let mut child = subject()
        .env("MINECRAFTTOOLS_LAUNCHER_PORT", handshake.port().to_string())
        .env("MINECRAFTTOOLS_LAUNCHER_TOKEN", handshake.token())
        .env("DEMO_SUBJECT_MODE", "never")
        .spawn()
        .unwrap();

    assert!(matches!(
        handshake.wait_for_ready(&mut child, Duration::from_millis(300)),
        Err(LaunchError::Timeout)
    ));
    let _ = child.kill();
    let _ = child.wait();
}

#[test]
fn wrong_token_is_not_accepted() {
    let handshake = Handshake::bind().unwrap();
    let mut child = subject()
        .env("MINECRAFTTOOLS_LAUNCHER_PORT", handshake.port().to_string())
        .env("MINECRAFTTOOLS_LAUNCHER_TOKEN", handshake.token())
        .env("DEMO_SUBJECT_MODE", "bad-token")
        .env("DEMO_SUBJECT_DELAY_MS", "40")
        .spawn()
        .unwrap();

    assert!(matches!(
        handshake.wait_for_ready(&mut child, Duration::from_millis(500)),
        Err(LaunchError::Timeout)
    ));
    let _ = child.kill();
    let _ = child.wait();
}
