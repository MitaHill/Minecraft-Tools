use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::diagnostics::{self, Report};
use crate::endpoints::Endpoints;
use crate::http::HttpClient;
use crate::install::{InstallError, InstallState};
use crate::launch::{Handshake, LaunchError};
use crate::manifest::{self, ManifestTrust};
use crate::network::{self, NetworkStatus};
use crate::package;
use crate::paths::Paths;
use crate::update::{self, HttpReleaseSource};

pub struct Context {
    pub paths: Paths,
    pub endpoints: Endpoints,
    pub client: HttpClient,
    pub release_trust: ManifestTrust,
    pub subject: PathBuf,
    pub bootstrap: Option<package::Bootstrap>,
    pub subject_args: Vec<String>,
    pub subject_env: Vec<(String, String)>,
    pub ready_timeout: Duration,
    pub health_attempts: u32,
    pub health_delay: Duration,
}

pub trait Reporter {
    fn status(&mut self, text: &str);
    fn progress(&mut self, progress: f32);
    fn hide_progress(&mut self);
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stage {
    Network,
    Health,
    UpdateCheck,
    InstallInfo,
    Download,
    Verify,
    Install,
    Launch,
    Ready,
}

impl Stage {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Network => "network",
            Self::Health => "health",
            Self::UpdateCheck => "update-check",
            Self::InstallInfo => "install-info",
            Self::Download => "download",
            Self::Verify => "verify",
            Self::Install => "install",
            Self::Launch => "launch",
            Self::Ready => "ready",
        }
    }
}

#[derive(Debug)]
pub struct Failure {
    pub stage: Stage,
    pub title: String,
    pub detail: String,
    pub exit_code: Option<i32>,
    pub can_retry: bool,
    pub report: Option<PathBuf>,
    pub upload_status: Option<String>,
    upload: bool,
}

impl Failure {
    fn new(
        stage: Stage,
        title: impl Into<String>,
        detail: impl Into<String>,
        can_retry: bool,
        upload: bool,
    ) -> Self {
        Self {
            stage,
            title: title.into(),
            detail: detail.into(),
            exit_code: None,
            can_retry,
            report: None,
            upload_status: None,
            upload,
        }
    }

    fn with_exit_code(mut self, exit_code: Option<i32>) -> Self {
        self.exit_code = exit_code;
        self
    }

    pub fn report_text(&self) -> String {
        let mut parts = Vec::new();
        if let Some(path) = &self.report {
            parts.push(format!("诊断报告：{}", path.display()));
        }
        if let Some(status) = &self.upload_status {
            parts.push(status.clone());
        }
        if parts.is_empty() {
            "没有可用的诊断报告。".into()
        } else {
            parts.join("\n")
        }
    }
}

pub fn run(context: &Context, reporter: &mut dyn Reporter) -> Result<(), Failure> {
    let mut log = Vec::new();
    match run_inner(context, reporter, &mut log) {
        Ok(()) => Ok(()),
        Err(mut failure) => {
            if !matches!(failure.stage, Stage::Network | Stage::Health) {
                finalize(context, &mut failure, &log);
            }
            Err(failure)
        }
    }
}

fn run_inner(
    context: &Context,
    reporter: &mut dyn Reporter,
    log: &mut Vec<String>,
) -> Result<(), Failure> {
    reporter.status("检查网络");
    log.push("检查本机网络".into());
    match network::probe(context.endpoints.host(), context.endpoints.port()) {
        NetworkStatus::Offline => {
            return Err(Failure::new(
                Stage::Network,
                "未检测到网络连接",
                "请连接网络后重新打开 Launcher。",
                false,
                false,
            ));
        }
        NetworkStatus::Online => log.push("网络可用".into()),
        NetworkStatus::Unknown => log.push("网络状态不明，继续检查总控制端".into()),
    }

    reporter.status("尝试连接服务器");
    let health_url = context.endpoints.health();
    log.push(format!("请求健康检查 {health_url}"));
    let health = network::wait_for_health(
        &context.client,
        &health_url,
        context.health_attempts,
        context.health_delay,
        |attempt, total, error| {
            log.push(format!("健康检查 {attempt}/{total} 失败：{error}"));
            reporter.status(&format!("尝试连接服务器（{attempt}/{total}）"));
        },
    );
    if let Err(error) = health {
        return Err(Failure::new(
            Stage::Health,
            "无法连接总控制端",
            format!("{error}\n请检查网络或稍后重试。"),
            false,
            false,
        ));
    }
    for message in std::mem::take(log) {
        note(log, context, message);
    }
    note(log, context, "总控制端连通");

    reporter.status("检查更新");
    reporter.hide_progress();
    let install_lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(context.paths.state.join("launcher.lock"))
        .map_err(|error| {
            Failure::new(
                Stage::Install,
                "无法锁定安装目录",
                error.to_string(),
                true,
                false,
            )
        })?;
    install_lock.try_lock().map_err(|error| {
        Failure::new(
            Stage::Install,
            "无法锁定安装目录",
            error.to_string(),
            true,
            false,
        )
    })?;
    let previous = match InstallState::load(&context.paths.install_file()) {
        Ok(install) => Some(install),
        Err(InstallError::Missing) if context.bootstrap.is_some() => None,
        Err(error) => {
            return Err(Failure::new(
                Stage::InstallInfo,
                "安装信息不可用",
                format!("{error}\n请重新下载并安装 MinecraftTools。"),
                false,
                false,
            ));
        }
    };
    let bootstrap = context.bootstrap.as_ref().filter(|initial| {
        previous.as_ref().is_none_or(|state| {
            initial.version > state.version
                || (initial.version == state.version && state.active_bundle.is_none())
        })
    });
    let install = if let Some(initial) = bootstrap {
        InstallState {
            channel: previous
                .as_ref()
                .map_or(initial.channel, |state| state.channel),
            version: initial.version,
            build_channel: initial.channel,
            active_bundle: None,
        }
    } else {
        previous.clone().expect("state or bootstrap was checked")
    };
    note(
        log,
        context,
        format!(
            "所选通道 {}，已安装 {}（{}）",
            install.channel, install.version, install.build_channel
        ),
    );

    let (os, arch) = match manifest::platform() {
        Ok(platform) => platform,
        Err(error) => {
            return Err(Failure::new(
                Stage::UpdateCheck,
                "当前平台不受支持",
                error,
                false,
                false,
            ));
        }
    };
    let source = HttpReleaseSource {
        client: &context.client,
        endpoints: &context.endpoints,
        trust: &context.release_trust,
    };
    let release = match update::find_update(&source, &install, os, arch) {
        Ok(release) => release,
        Err(error) => {
            return Err(Failure::new(
                Stage::UpdateCheck,
                "检查更新失败",
                error,
                false,
                false,
            ));
        }
    };

    let (candidate, build_channel, version) = if let Some(release) = release {
        note(
            log,
            context,
            format!("发现 {} 通道 {} 版本", release.channel, release.version),
        );
        reporter.status(&format!("下载更新 {}", release.version));
        reporter.progress(0.0);
        let download = tempfile::NamedTempFile::new_in(&context.paths.cache).map_err(|error| {
            Failure::new(
                Stage::Download,
                "无法创建更新缓存",
                error.to_string(),
                true,
                false,
            )
        })?;
        let downloaded =
            context
                .client
                .download(&release.url, download.path(), release.size, |read| {
                    let fraction = if release.size == 0 {
                        0.0
                    } else {
                        (read as f32 / release.size as f32).clamp(0.0, 1.0)
                    };
                    reporter.status(&format!("下载更新 {:.0}%", fraction * 100.0));
                    reporter.progress(fraction);
                });
        let (size, digest) = match downloaded {
            Ok(downloaded) => downloaded,
            Err(error) => {
                return Err(Failure::new(
                    Stage::Download,
                    "下载更新失败",
                    error,
                    true,
                    false,
                ));
            }
        };
        reporter.status("校验更新");
        reporter.progress(1.0);
        note(log, context, format!("下载完成：{size} 字节"));
        if size != release.size {
            return Err(Failure::new(
                Stage::Verify,
                "更新包大小不符",
                format!("预期 {} 字节，实际 {size} 字节。", release.size),
                true,
                false,
            ));
        }
        if !digest.eq_ignore_ascii_case(&release.sha256) {
            return Err(Failure::new(
                Stage::Verify,
                "更新包校验失败",
                "SHA-256 不一致，更新包可能不完整。",
                true,
                false,
            ));
        }

        reporter.status("正在安装更新");
        let candidate = package::stage(download.path(), &context.paths.versions(), release.version)
            .map_err(|error| {
                Failure::new(
                    Stage::Install,
                    "无法暂存更新",
                    error.to_string(),
                    true,
                    false,
                )
            })?;
        (candidate, release.channel, release.version)
    } else if let Some(initial) = bootstrap {
        reporter.status("正在准备已安装的主体程序");
        let candidate = initial.stage(&context.paths.versions()).map_err(|error| {
            Failure::new(
                Stage::Install,
                "无法准备安装包",
                error.to_string(),
                true,
                false,
            )
        })?;
        (candidate, initial.channel, initial.version)
    } else {
        reporter.status("已是最新版本");
        note(log, context, "没有更高版本");
        let subject = installed_subject(context, &install);
        return launch_subject(context, reporter, log, &install, &subject).map(|_| ());
    };
    let folder = candidate
        .path()
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| {
            Failure::new(
                Stage::Install,
                "无法暂存主体程序",
                "版本目录名称无效",
                true,
                false,
            )
        })?;
    let updated = install.with_update(build_channel, version, folder.to_owned());
    let subject = package::executable(candidate.path());
    let mut child = match launch_subject(context, reporter, log, &updated, &subject) {
        Ok(child) => child,
        Err(failure) => {
            drop(candidate);
            return Err(recover(context, reporter, log, previous.as_ref(), failure));
        }
    };
    let candidate = candidate.keep();
    if let Err(error) = updated.save(&context.paths.install_file()) {
        let _ = child.kill();
        let _ = child.wait();
        let _ = fs::remove_dir_all(candidate);
        let failure = Failure::new(Stage::Install, "安装更新失败", error, true, false);
        return Err(recover(context, reporter, log, previous.as_ref(), failure));
    }
    reporter.hide_progress();
    note(
        log,
        context,
        format!(
            "已记录安装版本 {}（{}）",
            updated.version, updated.build_channel
        ),
    );
    Ok(())
}

fn installed_subject(context: &Context, install: &InstallState) -> PathBuf {
    install.active_bundle.as_ref().map_or_else(
        || context.subject.clone(),
        |folder| package::executable(&context.paths.versions().join(folder)),
    )
}

fn recover(
    context: &Context,
    reporter: &mut dyn Reporter,
    log: &mut Vec<String>,
    previous: Option<&InstallState>,
    mut failure: Failure,
) -> Failure {
    let Some(install) = previous else {
        return failure;
    };
    let subject = installed_subject(context, install);
    if !subject.is_file() {
        return failure;
    }
    reporter.status("正在恢复已安装版本");
    note(
        log,
        context,
        format!("更新启动失败，恢复 {}", install.version),
    );
    match launch_subject(context, reporter, log, install, &subject) {
        Ok(_) => {
            failure
                .detail
                .push_str(&format!("\n已启动原版本 {}。", install.version));
            failure.can_retry = false;
        }
        Err(old_failure) => {
            failure
                .detail
                .push_str(&format!("\n原版本启动失败：{}", old_failure.detail));
        }
    }
    failure
}

fn launch_subject(
    context: &Context,
    reporter: &mut dyn Reporter,
    log: &mut Vec<String>,
    install: &InstallState,
    subject: &Path,
) -> Result<Child, Failure> {
    reporter.status("正在启动");
    reporter.hide_progress();
    note(log, context, format!("启动主体程序 {}", subject.display()));
    let handshake = match Handshake::bind() {
        Ok(handshake) => handshake,
        Err(error) => {
            return Err(Failure::new(
                Stage::Launch,
                "无法准备启动握手",
                error,
                true,
                true,
            ));
        }
    };
    let mut command = Command::new(subject);
    command
        .args(&context.subject_args)
        .envs(
            context
                .subject_env
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str())),
        )
        .env("MINECRAFTTOOLS_LAUNCHER_PORT", handshake.port().to_string())
        .env("MINECRAFTTOOLS_LAUNCHER_TOKEN", handshake.token())
        .env("MINECRAFTTOOLS_CONTROL_URL", context.endpoints.base())
        .env("MINECRAFTTOOLS_CLIENT_LOG", context.paths.client_log())
        .env("MINECRAFTTOOLS_CHANNEL", install.channel.as_str())
        .env("MINECRAFTTOOLS_VERSION", install.version.to_string())
        .env(
            "MINECRAFTTOOLS_BUILD_CHANNEL",
            install.build_channel.as_str(),
        );
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return Err(Failure::new(
                Stage::Launch,
                "无法启动主体程序",
                format!("{}：{error}", subject.display()),
                true,
                true,
            ));
        }
    };
    match handshake.wait_for_ready(&mut child, context.ready_timeout) {
        Ok(()) => {
            note(log, context, "主体程序已报告就绪");
            Ok(child)
        }
        Err(LaunchError::EarlyExit(exit_code)) => {
            let _ = child.wait();
            Err(Failure::new(
                Stage::Ready,
                "主体程序启动失败",
                "主体程序在报告就绪前退出。",
                true,
                true,
            )
            .with_exit_code(exit_code))
        }
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(Failure::new(
                Stage::Ready,
                "主体程序启动失败",
                error.to_string(),
                true,
                true,
            ))
        }
    }
}

fn finalize(context: &Context, failure: &mut Failure, log: &[String]) {
    let mut report = Report::new(failure.stage.as_str());
    if let Ok(install) = InstallState::load(&context.paths.install_file()) {
        report.channel = install.channel.to_string();
        report.build_channel = install.build_channel.to_string();
        report.version = install.version.to_string();
    }
    report.exit_code = failure.exit_code;
    report.log = diagnostics::sanitize(log);
    match diagnostics::save(&context.paths.logs, &report) {
        Ok(path) => failure.report = Some(path),
        Err(error) => failure
            .detail
            .push_str(&format!("\n无法保存诊断报告：{error}")),
    }
    if failure.upload {
        failure.upload_status = Some(
            match diagnostics::upload(&context.client, &context.endpoints.diagnostics(), &report) {
                Ok(()) => "诊断报告已上报。".into(),
                Err(error) => format!("诊断报告上报失败：{error}"),
            },
        );
    }
}

fn note(log: &mut Vec<String>, context: &Context, message: impl Into<String>) {
    let message = message.into();
    if log.len() >= 200 {
        log.remove(0);
    }
    log.push(message.clone());
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(context.paths.launcher_log())
    {
        let _ = writeln!(file, "{timestamp} {message}");
    }
}
