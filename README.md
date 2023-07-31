## Installation

```bash
brew install poetry libmagic ffmpeg
poetry self add poetry-dotenv-plugin
poetry install
```


## Todo

- whisper seems to struggle after a certain audio length, we might trim the audio to 5-10min chunks