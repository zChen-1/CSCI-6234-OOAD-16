# Traffic Camera Browser

## Development platform: macOS

## JSON files

### managed_cameras.json: camera list
### Traffic+Cameras.json: camera information from website of Arlington County
### users.json: users' information

## How to use

### app.py (macOS / Linux)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
### Recording helper (macOS / Linux)
```bash
python3 -m venv .venv
source .venv/bin/activate
brew install ffmpeg
python recorder_service.py
```

Open http://127.0.0.1:5000/

## Admin login
- username: `admin`
- password: `123456`
