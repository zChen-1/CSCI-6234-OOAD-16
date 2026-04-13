# Traffic Camera 

## Development platform: macOS

## How to use

### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg
python recorder_service.py
python app.py
```

Open http://127.0.0.1:5000/

## Admin login
- username: `admin`
- password: `123456`
