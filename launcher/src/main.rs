#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

slint::include_modules!();

use std::ffi::OsStr;
use std::fs;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use minecrafttools_launcher::demo::{self, MockServer};
use minecrafttools_launcher::demo_subject;
use minecrafttools_launcher::endpoints::Endpoints;
use minecrafttools_launcher::flow::{self, Context, Reporter};
use minecrafttools_launcher::http::HttpClient;
use minecrafttools_launcher::network;
use minecrafttools_launcher::paths::Paths;

const READY_TIMEOUT: Duration = Duration::from_secs(30);
const HTTP_TIMEOUT: Duration = Duration::from_secs(5);

struct Options {
    demo: bool,
    smoke_test: bool,
    local_fixture: bool,
    control_url: Option<String>,
    subject: Option<PathBuf>,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 演示模式回退到自身时，这个参数让当前进程扮演主体程序。
    if std::env::args_os().nth(1).as_deref() == Some(OsStr::new("--demo-subject")) {
        if let Err(error) = demo_subject::run() {
            eprintln!("{error}");
            std::process::exit(2);
        }
        return Ok(());
    }

    let options = match parse_args() {
        Ok(options) => options,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    };
    let (context, _demo_server) = build_context(&options)?;

    if options.smoke_test {
        flow::run(&context, &mut SmokeReporter)
            .map_err(|failure| format!("{}\n{}", failure.title, failure.detail))?;
        return Ok(());
    }

    let ui = SplashWindow::new()?;
    ui.on_exit_clicked(|| {
        let _ = slint::quit_event_loop();
    });
    let weak = ui.as_weak();
    let retry_weak = weak.clone();
    let retry_context = Arc::clone(&context);
    ui.on_retry_clicked(move || start(&retry_weak, Arc::clone(&retry_context)));
    start(&weak, context);
    ui.run()?;
    Ok(())
}

fn build_context(options: &Options) -> Result<(Arc<Context>, Option<MockServer>), String> {
    let client = HttpClient::new(HTTP_TIMEOUT);
    if options.demo {
        let root = std::env::temp_dir().join(format!(
            "minecrafttools-launcher-demo-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        let paths = Paths::from_root(root);
        paths.ensure()?;
        demo::initial_install().save(&paths.install_file())?;

        let executable =
            std::env::current_exe().map_err(|error| format!("无法定位 Launcher：{error}"))?;
        let sibling = sibling_binary("demo-subject")?;
        let (subject, subject_args) = if sibling.is_file() {
            (sibling, Vec::new())
        } else {
            (executable, vec!["--demo-subject".to_string()])
        };
        let server = MockServer::with_subject(&subject)?;
        let endpoints = Endpoints::new(server.base_url())?;
        println!("演示数据目录：{}", paths.state.display());
        println!("演示服务地址：{}", endpoints.base());
        let context = Context {
            paths,
            endpoints,
            client,
            release_trust: server.trust(),
            subject,
            bootstrap: None,
            subject_args,
            subject_env: Vec::new(),
            ready_timeout: READY_TIMEOUT,
            health_attempts: network::HEALTH_ATTEMPTS,
            health_delay: network::HEALTH_DELAY,
        };
        Ok((Arc::new(context), Some(server)))
    } else {
        let fixture = if options.local_fixture {
            if !options.smoke_test || std::env::var_os("MINECRAFTTOOLS_DATA_ROOT").is_none() {
                return Err("联调服务需要 --smoke-test 和独立数据目录".into());
            }
            Some(MockServer::empty()?)
        } else {
            None
        };
        let control_url = fixture
            .as_ref()
            .map(MockServer::base_url)
            .or(options.control_url.as_deref())
            .or(option_env!("MINECRAFTTOOLS_CONTROL_URL"))
            .ok_or_else(|| "请通过 --control-url 指定总控制端地址".to_string())?;
        let endpoints = Endpoints::new(control_url)?;
        let paths = Paths::discover()?;
        paths.ensure()?;
        let subject = match &options.subject {
            Some(path) => path.clone(),
            None => sibling_binary("MinecraftTools")?,
        };
        let bootstrap = if options.subject.is_none() {
            let launcher =
                std::env::current_exe().map_err(|error| format!("无法定位 Launcher：{error}"))?;
            minecrafttools_launcher::package::Bootstrap::discover(&launcher)?
        } else {
            None
        };
        let release_trust = fixture.as_ref().map_or_else(
            minecrafttools_launcher::manifest::ManifestTrust::embedded,
            |server| Ok(server.trust()),
        )?;
        let context = Context {
            paths,
            endpoints,
            client,
            release_trust,
            subject,
            bootstrap,
            subject_args: if options.smoke_test {
                vec!["--smoke-test".into()]
            } else {
                Vec::new()
            },
            subject_env: if options.smoke_test {
                vec![("QT_QPA_PLATFORM".into(), "offscreen".into())]
            } else {
                Vec::new()
            },
            ready_timeout: READY_TIMEOUT,
            health_attempts: network::HEALTH_ATTEMPTS,
            health_delay: network::HEALTH_DELAY,
        };
        Ok((Arc::new(context), fixture))
    }
}

struct SmokeReporter;

impl Reporter for SmokeReporter {
    fn status(&mut self, text: &str) {
        println!("{text}");
    }
    fn progress(&mut self, _progress: f32) {}
    fn hide_progress(&mut self) {}
}

fn start(ui: &slint::Weak<SplashWindow>, context: Arc<Context>) {
    if let Some(window) = ui.upgrade() {
        window.set_error_mode(false);
        window.set_can_retry(false);
        window.set_show_progress(true);
        window.set_progress(-1.0);
        window.set_status_text("检查网络".into());
    }
    let weak = ui.clone();
    std::thread::spawn(move || {
        let mut reporter = UiReporter { ui: weak.clone() };
        match flow::run(&context, &mut reporter) {
            Ok(()) => {
                let _ = weak.upgrade_in_event_loop(|window| {
                    let _ = window.hide();
                    let _ = slint::quit_event_loop();
                });
            }
            Err(failure) => {
                let _ = weak.upgrade_in_event_loop(move |window| show_failure(&window, failure));
            }
        }
    });
}

struct UiReporter {
    ui: slint::Weak<SplashWindow>,
}

impl Reporter for UiReporter {
    fn status(&mut self, text: &str) {
        let text = text.to_string();
        let _ = self.ui.upgrade_in_event_loop(move |window| {
            window.set_status_text(text.as_str().into());
            window.set_show_progress(true);
        });
    }

    fn progress(&mut self, progress: f32) {
        let _ = self.ui.upgrade_in_event_loop(move |window| {
            window.set_progress(progress);
            window.set_show_progress(true);
        });
    }

    fn hide_progress(&mut self) {
        let _ = self
            .ui
            .upgrade_in_event_loop(|window| window.set_show_progress(false));
    }
}

fn show_failure(window: &SplashWindow, failure: flow::Failure) {
    let report = failure.report_text();
    window.set_show_progress(false);
    window.set_error_mode(true);
    window.set_can_retry(failure.can_retry);
    window.set_error_title(failure.title.as_str().into());
    window.set_error_detail(failure.detail.as_str().into());
    window.set_error_report(report.as_str().into());
}

fn sibling_binary(name: &str) -> Result<PathBuf, String> {
    let executable =
        std::env::current_exe().map_err(|error| format!("无法定位 Launcher：{error}"))?;
    let directory = executable
        .parent()
        .ok_or_else(|| "无法定位 Launcher 目录".to_string())?;
    Ok(directory.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)))
}

fn parse_args() -> Result<Options, String> {
    let mut options = Options {
        demo: false,
        smoke_test: false,
        local_fixture: false,
        control_url: None,
        subject: None,
    };
    let mut arguments = std::env::args_os().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.to_string_lossy().as_ref() {
            "--demo" => options.demo = true,
            "--smoke-test" => options.smoke_test = true,
            "--local-fixture" => options.local_fixture = true,
            "--control-url" => {
                options.control_url = Some(
                    arguments
                        .next()
                        .ok_or_else(|| "--control-url 缺少参数".to_string())?
                        .to_string_lossy()
                        .into_owned(),
                );
            }
            "--subject" => {
                options.subject = Some(PathBuf::from(
                    arguments
                        .next()
                        .ok_or_else(|| "--subject 缺少参数".to_string())?,
                ));
            }
            "--help" | "-h" => {
                print_help();
                std::process::exit(0);
            }
            "--version" => {
                println!("{}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            other => return Err(format!("未知参数：{other}")),
        }
    }
    Ok(options)
}

fn print_help() {
    println!(
        "MinecraftTools Launcher 演示版\n\
         \n\
         用法：\n\
           launcher --demo\n\
           launcher --control-url https://example.com [--subject PATH]\n\
         \n\
         选项：\n\
           --demo              启动本地模拟服务，演示完整启动流程\n\
           --control-url URL   总控制端地址（非演示模式必填）\n\
           --subject PATH      主体程序路径，默认为 Launcher 同目录的 MinecraftTools\n\
           -h, --help          显示帮助\n\
           --version           显示版本"
    );
}
