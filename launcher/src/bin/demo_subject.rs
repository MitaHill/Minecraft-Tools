fn main() {
    if let Err(error) = minecrafttools_launcher::demo_subject::run() {
        eprintln!("demo-subject: {error}");
        std::process::exit(2);
    }
}
