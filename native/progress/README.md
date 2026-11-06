# Optional Rust history reducer

This PyO3 extension implements the pure training-history reduction used by
`postern.progress.history`. Python owns row selection and every durable effect.
See the [migration runbook](../../docs/runbooks/rust-server.md) for measurements,
compatibility contracts, build/test commands, acceptance targets and rollback.

The separate wheel is opt-in: ordinary Postern installs use Python and do not
build Rust. `POSTERN_PROGRESS_BACKEND=rust` requires a matching installed wheel;
`python` is the default and rollback setting. The native API is internal and
versioned independently of the Python package through `API_VERSION` and
`CHECKPOINT_VERSION`. Wheel 0.2.0 adds the bounded incremental evidence interface;
the matching application refuses an older wheel before starting its writer.

Keep `Cargo.lock` and `rust-toolchain.toml` with releases. Build with the pinned
toolchain and locked dependency graph. Do not distribute `target/`, developer
build artifacts or a wheel for a different platform.
