use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use minecrafttools_launcher::segmented::{self, Config};

fn main() {
    let mut arguments = std::env::args().skip(1);
    let Some(url) = arguments.next() else {
        eprintln!(
            "用法：segmented_download <URL> <输出文件> [最大连接数] [分片大小 MiB] [爬坡间隔 ms]"
        );
        std::process::exit(2);
    };
    let Some(output) = arguments.next() else {
        eprintln!("缺少输出文件");
        std::process::exit(2);
    };
    let max_connections = arguments
        .next()
        .and_then(|value| value.parse().ok())
        .unwrap_or(6);
    let chunk_mib = arguments
        .next()
        .and_then(|value| value.parse().ok())
        .unwrap_or(4);
    let ramp_ms = arguments
        .next()
        .and_then(|value| value.parse().ok())
        .unwrap_or(500);
    let config = Config {
        max_connections,
        chunk_size: chunk_mib * 1024 * 1024,
        ramp_interval: Duration::from_millis(ramp_ms),
        ..Config::default()
    };

    let started = Instant::now();
    let last_percent = Arc::new(AtomicU64::new(0));
    let progress_percent = Arc::clone(&last_percent);
    let result = segmented::download(
        &url,
        &PathBuf::from(&output),
        &config,
        move |done, total| {
            let percent = done.saturating_mul(100).checked_div(total).unwrap_or(0);
            let previous = progress_percent.swap(percent, Ordering::Relaxed);
            if percent >= previous + 5 || percent == 100 {
                eprintln!(
                    "{percent:3}%  {done}/{total}  {:.1}s",
                    started.elapsed().as_secs_f32()
                );
            }
        },
    );

    match result {
        Ok(summary) => {
            println!("bytes={}", summary.bytes);
            println!("sha256={}", summary.sha256);
            println!("fallback={}", summary.fallback);
            println!("connections_peak={}", summary.connections_peak);
            println!("ramp={:?}", summary.ramp);
            println!("elapsed={:.2}s", started.elapsed().as_secs_f32());
        }
        Err(error) => {
            eprintln!("下载失败：{error}");
            std::process::exit(1);
        }
    }
}
