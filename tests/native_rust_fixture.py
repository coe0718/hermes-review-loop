"""Small pinned dependency fixture, staged by the host before sandbox execution."""
from pathlib import Path
import json
import sys


def stage(root: Path, *, secret: Path | None = None, vendor: Path | None = None):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'src').mkdir(exist_ok=True)
    (root / 'Cargo.toml').write_text('''[package]
name = "native-seatbelt-fixture"
version = "0.1.0"
edition = "2021"

[dependencies]
memchr = "=2.7.4"
''')
    (root / 'src/lib.rs').write_text('''pub fn find_byte(data: &[u8], byte: u8) -> Option<usize> {
    memchr::memchr(byte, data)
}

#[test]
fn vendored_dependency_works() {
    assert_eq!(find_byte(b"seatbelt", b'b'), Some(4));
    assert_eq!(find_byte(b"seatbelt", b'z'), None);
}
''')
    if secret is not None and vendor is not None:
        # Rust string literals use JSON-compatible escaping for these generated paths.
        (root / 'build.rs').write_text('''use std::{fs, io::ErrorKind, net::TcpStream};
fn main() {
    assert_eq!(fs::read_to_string(__SECRET_PATH__).unwrap_err().kind(), ErrorKind::PermissionDenied);
    assert_eq!(TcpStream::connect("127.0.0.1:9").unwrap_err().kind(), ErrorKind::PermissionDenied);
    assert_eq!(fs::OpenOptions::new().write(true).open(__VENDOR_FILE__).unwrap_err().kind(),
               ErrorKind::PermissionDenied);
    assert!(std::env::var_os("GITHUB_TOKEN").is_none());
    println!("cargo:warning=RUST_HOST_SECRET_BLOCKED");
    println!("cargo:warning=RUST_NETWORK_BLOCKED");
    println!("cargo:warning=RUST_VENDOR_WRITE_BLOCKED");
}
'''.replace('__SECRET_PATH__', json.dumps(str(secret))).replace('__VENDOR_FILE__', json.dumps(str(vendor / 'memchr/Cargo.toml'))))


if __name__ == '__main__':
    stage(Path(sys.argv[1]))
