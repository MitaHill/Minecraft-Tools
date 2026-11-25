use std::time::Duration;

use minecrafttools_launcher::demo::{self, MockServer};
use minecrafttools_launcher::http::HttpClient;
use minecrafttools_launcher::install::Channel;

#[test]
fn serves_health_and_manifests() {
    let server = MockServer::start().unwrap();
    let client = HttpClient::new(Duration::from_secs(5));

    assert_eq!(
        client
            .get_status(&format!("{}/health", server.base_url()))
            .unwrap(),
        200
    );
    let text = client
        .get_text(&format!("{}/releases/dev.json", server.base_url()))
        .unwrap();
    let manifest = server.trust().verify(&text, Channel::Dev).unwrap();
    assert_eq!(manifest.channel, Channel::Dev);
    assert!(
        manifest
            .releases
            .iter()
            .any(|release| release.version.to_string() == demo::DEMO_DEV)
    );
}

#[test]
fn downloads_and_hashes_the_artifact() {
    let server = MockServer::start().unwrap();
    let client = HttpClient::new(Duration::from_secs(5));
    let text = client
        .get_text(&format!("{}/releases/stable.json", server.base_url()))
        .unwrap();
    let manifest = server.trust().verify(&text, Channel::Stable).unwrap();
    let release = manifest
        .releases
        .iter()
        .find(|release| release.version.to_string() == demo::DEMO_STABLE)
        .unwrap();

    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("update.part");
    let (size, digest) = client
        .download(&release.url, &destination, release.size, |_| {})
        .unwrap();

    assert_eq!(size, release.size);
    assert_eq!(digest, release.sha256);
    assert_eq!(std::fs::metadata(&destination).unwrap().len(), release.size);
}

#[test]
fn reports_missing_routes_and_accepts_diagnostics() {
    let server = MockServer::start().unwrap();
    let client = HttpClient::new(Duration::from_secs(5));

    assert_eq!(
        client
            .get_status(&format!("{}/missing", server.base_url()))
            .unwrap(),
        404
    );
    client
        .post_json(
            &format!("{}/diagnostics", server.base_url()),
            r#"{"stage":"ready"}"#,
        )
        .unwrap();
    assert_eq!(server.diagnostics_received(), 1);
}
