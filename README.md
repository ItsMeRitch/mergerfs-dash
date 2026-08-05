# mergerfs-dash

A tiny, zero-dependency web dashboard that shows how your data is distributed
across the branches of a [mergerfs](https://github.com/trapexit/mergerfs) pool.

- Used/total/free per branch, plus a balance meter (fill-% spread)
- Which branch each top-level share's bytes actually live on
- Breakdown by file type (video / audio / images / docs / archives / other)
- Largest files in the pool and per-branch file/dir/inode counts
- Deep stats come from a background scan that runs **once**, is cached to
  disk, and only re-runs when you press **Rescan** in the UI

The app is a single Python file using only the standard library. It is
completely read-only.

## Run with Docker (recommended)

A prebuilt multi-arch image (`linux/amd64` + `linux/arm64`) is available at
`ghcr.io/itsmeritch/mergerfs-dash`. On your server:

```bash
# copy compose.example.yml to your server as docker-compose.yml,
# edit the branch paths to match your pool, then:
docker compose pull
docker compose up -d
# dashboard: http://your-server:8282
```

## Run without Docker

```bash
python3 mergerfs_dash.py --mount /storage
# or with explicit branches:
python3 mergerfs_dash.py --branches /mnt/disk1,/mnt/disk2 --port 8282
```

A systemd unit example is in the header comment of `mergerfs_dash.py`.

## Configuration

| Setting    | CLI flag      | Env var      | Default                                |
|------------|---------------|--------------|----------------------------------------|
| Branches   | `--branches`  | `BRANCHES`   | auto-detected from `--mount` via xattr |
| Mount      | `--mount`     | `MOUNT`      | —                                      |
| Port       | `--port`      | `PORT`       | `8282`                                 |
| Listen addr| `--host`      | `HOST`       | `0.0.0.0`                              |
| Scan cache | `--cache`     | `CACHE_PATH` | next to the script / `/data/…` in Docker |
