# Prediction interface for Cog ⚙️
from cog import BasePredictor, Input, File, BaseModel
import os
import time
import json
import wave
import torch
import base64
from faster_whisper import WhisperModel
import datetime
import contextlib
import requests
import numpy as np
import pandas as pd
from pyannote.audio import Audio
from pyannote.core import Segment
from sklearn.cluster import AgglomerativeClustering
from pyannote.audio.pipelines.speaker_verification import PretrainedSpeakerEmbedding
from typing import Any
import mimetypes
import magic


class ModelOutput(BaseModel):
    segments: Any


class Predictor(BasePredictor):

    def setup(self):
        """Load the model into memory to make running multiple predictions efficient"""
        model_name = "bofenghuang/whisper-large-v2-cv11-french-ct2"
        
        if torch.cuda.is_available():
            self.model = WhisperModel(model_name, device="cuda", compute_type="float16")
        else:
            self.model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=6)

        device = "cuda" if torch.cuda.is_available() else (
            "mps" if torch.backends.mps.is_available() else "cpu"
        )
        self.embedding_model = PretrainedSpeakerEmbedding(
            "speechbrain/spkrec-ecapa-voxceleb",
            device
        )

    def predict(
        self,
        file_string: str = Input(description="Either provide: Base64 encoded audio file,",
                                 default=None),
        file_url: str = Input(description="Or provide: A direct audio file URL", default=None),
        # file: File = Input(description="An audio file", default=None), not implemented yet
        group_segments: bool = Input(description="Group segments of same speaker shorter apart than 2 seconds", default=True),
        num_speakers: int = Input(description="Number of speakers",
                                  ge=1,
                                  le=50,
                                  default=2),
        prompt: str = Input(description="Prompt, to be used as context",
                            default=""),
        offset_seconds: int = Input(
            description="Offset in seconds, used for chunked inputs",
            default=0,
            ge=0)
    ) -> ModelOutput:
        """Run a single prediction on the model"""
        # Check if either filestring, filepath or file is provided, but only 1 of them
        if sum([
                file_string is not None, file_url is not None
        ]) != 1:
            raise RuntimeError("Provide either file_string or file_url")

        """ filepath = ''
        file_start, file_ending = os.path.splitext(f'{filename}')"""
        ts = time.time()
        filename = f'{ts}-recording' 
        file_extension = '.mp3'

        # If filestring is provided, save it to a file
        if file_string is not None and file_url is None:
            base64file = file_string.split(
                ',')[1] if ',' in file_string else file_string
            file_data = base64.b64decode(base64file)
            mime_type = magic.from_buffer(file_data, mime=True)
            file_extension = mimetypes.guess_extension(mime_type)
            filename += file_extension if file_extension else ''
            with open(filename, 'wb') as f:
                f.write(file_data)

        # If file_url is provided, download the file from url
        if file_string is None and file_url is not None:
            response_head = requests.head(file_url)
            if 'Content-Type' in response_head.headers:
                mime_type = response_head.headers['Content-Type']
                file_extension = mimetypes.guess_extension(mime_type)
            response = requests.get(file_url)
            filename += file_extension if file_extension else ''
            with open(filename, 'wb') as file:
                file.write(response.content)


        filepath = filename
        segments = self.speech_to_text(filepath, num_speakers, prompt,
                                            offset_seconds, group_segments)
        print(f'done with creating segments')

        if file_extension != 'wav':
            print("removing non wav file")
            os.remove(filepath)

        print(f'done with inference')
        # Return the results as a JSON object
        return ModelOutput(segments=segments)

    def convert_time(self, secs, offset_seconds=0):
        return datetime.timedelta(seconds=(secs) + offset_seconds)

    def speech_to_text(self, filepath, num_speakers=2, prompt="", offset_seconds=0, group_segments=True):
        # model = whisper.load_model('large-v2')
        time_start = time.time()

        try:
            _, file_ending = os.path.splitext(f'{filepath}')
            print(f'file ending in {file_ending}')
            if file_ending != '.wav':
                audio_file_wav = filepath.replace(file_ending, ".wav")
                print("-----starting conversion to wav-----")
                os.system(
                    f'ffmpeg -i "{filepath}" -ar 16000 -ac 1 -c:a pcm_s16le "{audio_file_wav}"'
                )
            else:
                audio_file_wav = filepath
        except Exception as e:
            raise RuntimeError("Error converting audio")

        # Get duration
        with contextlib.closing(wave.open(audio_file_wav, 'r')) as f:
            frames = f.getnframes()
            rate = f.getframerate()
            duration = frames / float(rate)
        print(f"conversion to wav ready, duration of audio file: {duration}")

        # Transcribe audio
        print("starting whisper")
        if torch.cuda.is_available():
            params=dict()
        else:
            params=dict(
                beam_size=2,
                best_of=5
            )
        raw_segments, info = self.model.transcribe(
            audio_file_wav,
            initial_prompt=prompt,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(window_size_samples=1536),
            **params
        )
        
        print("Detected language '%s' with probability %f" % (info.language, info.language_probability))
        segments = []

        for s in raw_segments:
            if s.no_speech_prob > 0.75:
                continue
            segments.append(s)

        segments = [{
            'start': s.start,
            'end': s.end,
            'text': s.text
        } for s in segments]
        
        print("done with whisper")

        try:
            # Create embedding
            def segment_embedding(segment):
                audio = Audio()
                start = segment["start"]
                # Whisper overshoots the end timestamp in the last segment
                end = min(duration, segment["end"])
                clip = Segment(start, end)
                waveform, sample_rate = audio.crop(audio_file_wav, clip)
                return self.embedding_model(waveform[None])

            print("starting embedding")
            embeddings = np.zeros(shape=(len(segments), 192))
            for i, segment in enumerate(segments):
                embeddings[i] = segment_embedding(segment)
            embeddings = np.nan_to_num(embeddings)
            print(f'Embedding shape: {embeddings.shape}')

            # Assign speaker label
            clustering = AgglomerativeClustering(num_speakers).fit(embeddings)
            labels = clustering.labels_
            for i in range(len(segments)):
                segments[i]["speaker"] = 'SPEAKER ' + str(labels[i] + 1)

            # Make output
            output = []  # Initialize an empty list for the output

            # Initialize the first group with the first segment
            current_group = {
                'start': str(segments[0]["start"] + offset_seconds),
                'end': str(segments[0]["end"] + offset_seconds),
                'speaker': segments[0]["speaker"],
                'text': segments[0]["text"]
            }

            for i in range(1, len(segments)):
                # Calculate time gap between consecutive segments
                time_gap = segments[i]["start"] - segments[i - 1]["end"]

                # If the current segment's speaker is the same as the previous segment's speaker, and the time gap is less than or equal to 2 seconds, group them
                if segments[i]["speaker"] == segments[
                        i - 1]["speaker"] and time_gap <= 2 and group_segments:
                    current_group["end"] = str(
                        segments[i]["end"] + offset_seconds)
                    current_group["text"] += " " + segments[i]["text"]
                else:
                    # Add the current_group to the output list
                    output.append(current_group)

                    # Start a new group with the current segment
                    current_group = {
                        'start':
                        str(segments[i]["start"] + offset_seconds),
                        'end': str(segments[i]["end"] + offset_seconds),
                        'speaker': segments[i]["speaker"],
                        'text': segments[i]["text"]
                    }

            # Add the last group to the output list
            output.append(current_group)

            print("done with embedding")
            time_end = time.time()
            time_diff = time_end - time_start

            system_info = f"""-----Processing time: {time_diff:.5} seconds-----"""
            print(system_info)
            os.remove(audio_file_wav)
            return output

        except Exception as e:
            os.remove(audio_file_wav)
            raise RuntimeError("Error Running inference with local model", e)