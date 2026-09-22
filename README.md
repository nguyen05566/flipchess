# flipchess

Cờ Úp Bot (Mystery Xiangqi / 揭棋) dùng engine **PikaJieQi** từ [mistboard](https://github.com/brianhliou/pikafish-jieqi-wasm).

## Cách hoạt động

```
Server gamevh.net ─── WS frame ───> ws_frame_dump.py
                                       │
                                       └─ raw_face byte = true piece type
                                          (bot biết quân úp là gì!)
                                       │
                                       ▼
                                  cup_bot_flipchess.py
                                  → gửi `position startpos moves c3c4N e6e5P ...`
                                    (suffix = reveal piece type)
                                       │
                                       ▼
                                  PikaJieQi engine (UCI)
                                  → search → bestmove
```

## Files

| File | Mô tả |
|---|---|
| `cup_bot_flipchess.py` | Bot chính — login, tạo bàn, đi nước |
| `ws_frame_dump.py` | Parse WS frame từ server, track reveal piece |
| `PikaJieQi` | Engine binary (download tự động trong workflow) |
| `pikafish.nnue` | NNUE weights (download tự động trong workflow) |

## Chạy locally

```bash
pip install websocket-client

# Download engine
curl -sL -o pikajieqi.tar.gz \
  https://github.com/brianhliou/pikafish-jieqi-wasm/releases/download/mistboard-2026-09-20/pikajieqi-linux-x86-64.tar.gz
tar xzf pikajieqi.tar.gz PikaJieQi
chmod +x PikaJieQi

# Download NNUE
curl -sL -o pikafish.nnue \
  https://github.com/official-pikafish/Networks/releases/download/master-net/pikafish.nnue

# Set credentials
export CARO_USER19="your_username"
export CARO_PASSWD19="your_password"

# Run
python3 -u cup_bot_flipchess.py
```

## Chạy via GitHub Actions

1. Fork repo này
2. Vào **Settings → Secrets and variables → Actions**
3. Thêm 2 secrets:
   - `CARO_USER19` = tài khoản gamevh.net
   - `CARO_PASSWD19` = mật khẩu
4. Vào **Actions → Run Bot → Run workflow**

Bot sẽ chạy tối đa 6 giờ (GitHub Actions timeout), tự động tạo bàn 1000 xu và chơi.

## Cấu hình

| Biến | Mặc định | Mô tả |
|---|---|---|
| `BOT_BET_XU` | 1000 | Mức cược (xu) |
| `BOT_USE_CREATE_TABLE` | True | Tự tạo bàn thay vì quick play |
| `MIN_MOVE_SECONDS` | 3.0 | Thời gian tối thiểu mỗi nước |
| `MOVE_DEADLINE_SECONDS` | 30.0 | Deadline tối đa mỗi nước |
| `MAX_ENGINE_RESTARTS_PER_GAME` | 2 | Số lần restart engine tối đa/ván |

## Engine

- **Repo**: https://github.com/brianhliou/pikafish-jieqi-wasm
- **Branch**: `jieqi_old-mistboard`
- **Release**: `mistboard-2026-09-20`
- **License**: GPL-3.0

## License

GPL-3.0 (kế thừa từ Pikafish/PikaJieQi)
