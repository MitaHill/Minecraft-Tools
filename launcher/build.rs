fn main() {
    slint_build::compile("ui/splash.slint").expect("Slint build failed");
    println!("cargo:rerun-if-changed=ui/splash.png");
}
