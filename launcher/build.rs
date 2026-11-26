fn main() {
    slint_build::compile("ui/splash.slint").expect("Slint build failed");
    println!("cargo:rerun-if-changed=ui/splash.png");
    println!("cargo:rerun-if-changed=../client/src/minecrafttools/logo-unified.ico");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        winresource::WindowsResource::new()
            .set_icon("../client/src/minecrafttools/logo-unified.ico")
            .compile()
            .expect("Windows icon resource compilation failed");
    }
}
