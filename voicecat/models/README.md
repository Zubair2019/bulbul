# Local speech models (optional)

Docker mounts this folder into the backend. Voice works without anything here: with a
`DEEPGRAM_API_KEY` in `.env` it uses Deepgram, and with nothing at all it uses the browser's own
speech (Chrome).

For free, local speech, put these files here (see `../README.md`, "Speech providers"):

- `silero_vad.onnx`: better detection of when you stop talking, from the silero-vad project.
- `kokoro-v1.0.int8.onnx` and `voices-v1.0.bin`: the Kokoro voice, from the kokoro-onnx GitHub
  releases (`model-files-v1.0`).
- `whisper/`: created automatically the first time Whisper is used.

Everything in this folder except this README is ignored by git.
