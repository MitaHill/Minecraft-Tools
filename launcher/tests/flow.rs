use std::path::{Path, PathBuf};
use std::time::Duration;

use minecrafttools_launcher::demo::{self, MockServer};
use minecrafttools_launcher::endpoints::Endpoints;
use minecrafttools_launcher::flow::{self, Context, Reporter, Stage};
use minecrafttools_launcher::http::HttpClient;
use minecrafttools_launcher::install::{Channel, InstallState};
use minecrafttools_launcher::paths::Paths;

#[derive(Default)]
struct TestReporter {
    statuses: Vec<String>,
}

impl Reporter for TestReporter {
    fn status(&mut self, text: &str) {
        self.statuses.push(text.to_string());
    }

    fn progress(&mut self, _progress: f32) {}

    fn hide_progress(&mut self) {}
}

fn setup(root: &Path, server: &MockServer, subject_env: &[(&str, &str)]) -> Context {
    let paths = Paths::from_root(root);
    paths.ensure().unwrap();
    demo::initial_install().save(&paths.install_file()).unwrap();
    Context {
        paths,
        endpoints: Endpoints::new(server.base_url()).unwrap(),
        client: HttpClient::new(Duration::from_secs(5)),
        release_trust: server.trust(),
        subject: PathBuf::from(env!("CARGO_BIN_EXE_demo-subject")),
        bootstrap: None,
        subject_args: Vec::new(),
        subject_env: subject_env
            .iter()
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect(),
        ready_timeout: Duration::from_secs(5),
        health_attempts: 3,
        health_delay: Duration::from_millis(20),
    }
}

#[test]
fn demo_flow_updates_then_starts_the_subject() {
    let server = MockServer::with_subject(Path::new(env!("CARGO_BIN_EXE_demo-subject"))).unwrap();
    let directory = tempfile::tempdir().unwrap();
    let context = setup(
        directory.path(),
        &server,
        &[
            ("DEMO_SUBJECT_DELAY_MS", "40"),
            ("DEMO_SUBJECT_LIFETIME_MS", "100"),
        ],
    );
    let mut reporter = TestReporter::default();

    flow::run(&context, &mut reporter).unwrap();

    let installed = InstallState::load(&context.paths.install_file()).unwrap();
    assert_eq!(installed.channel, Channel::Dev);
    assert_eq!(installed.build_channel, Channel::Dev);
    assert_eq!(installed.version.to_string(), demo::DEMO_DEV);
    let active = context
        .paths
        .versions()
        .join(installed.active_bundle.unwrap());
    assert!(minecrafttools_launcher::package::executable(&active).is_file());
    assert!(
        reporter
            .statuses
            .iter()
            .any(|status| status.starts_with("下载更新"))
    );
    assert!(reporter.statuses.iter().any(|status| status == "正在启动"));
    assert!(context.paths.client_log().is_file());
    assert_eq!(server.diagnostics_received(), 0);
    assert!(
        std::fs::read_to_string(context.paths.launcher_log())
            .unwrap()
            .contains(&active.display().to_string())
    );
}

#[test]
fn early_exit_saves_and_uploads_diagnostics() {
    let server = MockServer::with_subject(Path::new(env!("CARGO_BIN_EXE_demo-subject"))).unwrap();
    let directory = tempfile::tempdir().unwrap();
    let context = setup(directory.path(), &server, &[("DEMO_SUBJECT_MODE", "exit")]);
    let mut reporter = TestReporter::default();

    let failure = flow::run(&context, &mut reporter).unwrap_err();

    assert_eq!(failure.stage, Stage::Ready);
    assert_eq!(failure.exit_code, Some(1));
    assert!(failure.can_retry);
    let report = failure.report.as_ref().expect("report should be saved");
    assert!(report.is_file());
    let text = std::fs::read_to_string(report).unwrap();
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    assert_eq!(value["stage"], "ready");
    assert_eq!(value["channel"], "dev");
    assert_eq!(server.diagnostics_received(), 1);
    assert!(failure.report_text().contains("诊断报告"));
}

#[test]
fn failing_candidate_recovers_previous_install() {
    let server = MockServer::with_subject(Path::new(env!("CARGO_BIN_EXE_demo-subject"))).unwrap();
    for mode in ["exit", "never"] {
        let directory = tempfile::tempdir().unwrap();
        let mut context = setup(
            directory.path(),
            &server,
            &[
                ("DEMO_SUBJECT_MODE", mode),
                ("DEMO_SUBJECT_APPLY_MODE_TO_VERSION", demo::DEMO_DEV),
                ("DEMO_SUBJECT_DELAY_MS", "10"),
                ("DEMO_SUBJECT_LIFETIME_MS", "1000"),
            ],
        );
        context.ready_timeout = Duration::from_secs(2);
        let mut reporter = TestReporter::default();

        let error = flow::run(&context, &mut reporter).unwrap_err();

        assert_eq!(error.stage, Stage::Ready);
        assert!(
            error
                .detail
                .contains(&format!("已启动原版本 {}", demo::DEMO_INSTALLED)),
            "{mode}: {error:?}; log: {:?}",
            std::fs::read_to_string(context.paths.client_log())
        );
        assert_eq!(
            InstallState::load(&context.paths.install_file()).unwrap(),
            demo::initial_install()
        );
        assert_eq!(
            std::fs::read_dir(context.paths.versions()).unwrap().count(),
            0
        );
        assert_eq!(std::fs::read_dir(&context.paths.cache).unwrap().count(), 0);
        let log = std::fs::read_to_string(context.paths.launcher_log()).unwrap();
        assert!(log.contains("主体程序已报告就绪"));
    }
    assert_eq!(server.diagnostics_received(), 2);
}

#[test]
fn a_signed_but_damaged_archive_preserves_install_state() {
    let server = MockServer::with_artifact(b"invalid archive".to_vec()).unwrap();
    let directory = tempfile::tempdir().unwrap();
    let context = setup(directory.path(), &server, &[]);
    let failure = flow::run(&context, &mut TestReporter::default()).unwrap_err();
    assert_eq!(failure.stage, Stage::Install);
    assert_eq!(
        InstallState::load(&context.paths.install_file()).unwrap(),
        demo::initial_install()
    );
    assert_eq!(
        std::fs::read_dir(context.paths.versions()).unwrap().count(),
        0
    );
    assert_eq!(server.diagnostics_received(), 0);
}

#[test]
fn health_failure_does_not_write_or_upload_logs() {
    let server = MockServer::unavailable().unwrap();
    let directory = tempfile::tempdir().unwrap();
    let context = setup(directory.path(), &server, &[]);
    let failure = flow::run(&context, &mut TestReporter::default()).unwrap_err();
    assert_eq!(failure.stage, Stage::Health);
    assert!(failure.report.is_none());
    assert_eq!(std::fs::read_dir(&context.paths.logs).unwrap().count(), 0);
    assert_eq!(server.diagnostics_received(), 0);
}
