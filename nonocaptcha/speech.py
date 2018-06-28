#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Text-to-speech modules"""

import aiobotocore
import asyncio
import json
import re
import struct
import time
import websockets

from io import StringIO, BytesIO
from datetime import datetime
from uuid import uuid4
from pydub import AudioSegment

from config import settings
from nonocaptcha import util


class Amazon(object):
    ACCESS_KEY_ID = settings['speech_api']['amazon']['key_id']
    SECRET_ACCESS_KEY = settings['speech_api']['amazon']['secret_access_key']
    REGION_NAME = settings['speech_api']['amazon']['region']
    S3_BUCKET = settings['speech_api']['amazon']['s3_bucket']

    async def get_text(self, data):
        session = aiobotocore.get_session()
        upload = session.create_client(
            's3',
            region_name=self.REGION_NAME,
            aws_secret_access_key=self.SECRET_ACCESS_KEY,
            aws_access_key_id=self.ACCESS_KEY_ID
        )                          
        transcribe = session.create_client(
            'transcribe', 
            region_name=self.REGION_NAME,
            aws_secret_access_key=self.SECRET_ACCESS_KEY,
            aws_access_key_id=self.ACCESS_KEY_ID
        )
        filename = f"{uuid4().hex}.mp3"
        # Upload audio file to bucket
        await upload.put_object(Bucket=self.S3_BUCKET,
                                Key=filename,
                                Body=data)
        job_name = uuid4().hex
        job_uri = (
            f"https://s3.{self.REGION_NAME}.amazonaws.com/{self.S3_BUCKET}/"
            f"{filename}"
        )
        # Send audio file URI to Transcribe
        resp = await transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            Media={'MediaFileUri': job_uri},
            MediaFormat='mp3',
            LanguageCode='en-US'
        )
        # Wait 60 seconds for transcription
        timeout = 60
        while time.time() > timeout:
            status = await transcribe.get_transcription_job(
                TranscriptionJobName=job_name
            )
            if(
                status['TranscriptionJob']['TranscriptionJobStatus'] in
                ['COMPLETED', 'FAILED']
            ):
                break
            await asyncio.sleep(1)
        # Delete audio file from bucket
        await upload.delete_object(Bucket=self.S3_BUCKET, Key=filename)
        if 'TranscriptFileUri' in status['TranscriptionJob']['Transcript']:
            transcript_uri = (
                status['TranscriptionJob']['Transcript']['TranscriptFileUri']
            )
            data = json.loads(await util.get_page(transcript_uri))
            transcript = data['results']['transcripts'][0]['transcript']
            return transcript


class Azure(object):
    SUB_KEY = settings['speech_api']['azure']["api_subkey"]

    @util.threaded
    def bytes_from_file(self, filename, chunksize=8192):
        with open(filename, "rb") as f:
            while True:
                chunk = f.read(chunksize)
                if chunk:
                    yield chunk
                else:
                    break

    @util.threaded
    def mp3_to_wav(self, mp3_filename):
        wav_filename = mp3_filename.replace(".mp3", ".wav")
        sound = AudioSegment.from_mp3(mp3_filename)
        wav = sound.export(wav_filename, format="wav")
        return wav_filename

    @util.threaded
    def extract_json_body(self, response):
        pattern = "^\r\n"  # header separator is an empty line
        m = re.search(pattern, response, re.M)
        return json.loads(
            response[m.end():]
        )  # assuming that content type is json

    @util.threaded
    def build_message(self, req_id, payload):
        message = b""
        timestamp = datetime.utcnow().isoformat()
        header = (
            f"X-RequestId: {req_id}\r\nX-Timestamp: {timestamp}Z\r\n"
            f"Path: audio\r\nContent-Type: audio/x-wav\r\n\r\n"
        )
        message += struct.pack(">H", len(header))
        message += header.encode()
        message += payload
        return message

    async def send_file(self, websocket, filename):
        req_id = uuid4().hex
        for payload in await self.bytes_from_file(filename):
            message = await self.build_message(req_id, payload)
            await websocket.send(message)

    async def get_text(self, mp3_filename):
        wav_filename = await self.mp3_to_wav(self.mp3_filename)
        conn_id = uuid4().hex
        url = (
            f"wss://speech.platform.bing.com/speech/recognition/dictation/cogn"
            f"itiveservices/v1?language=en-US&Ocp-Apim-Subscription-Key="
            f"{self.SUB_KEY}&X-ConnectionId={conn_id}&format=detailed"
        )
        async with websockets.connect(url) as websocket:
            await self.send_file(websocket, wav_filename)
            timeout = time.time() + 15
            while time.time() < timeout:
                response = await websocket.recv()
                content = await self.extract_json_body(response)
                if (
                    "RecognitionStatus" in content
                    and content["RecognitionStatus"] == "Success"
                ):
                    answer = content["NBest"][0]["Display"]
                    return answer[:-1].lower()
                if (
                    "RecognitionStatus" in content
                    and content["RecognitionStatus"] == "EndOfDictation"
                ):
                    return
                await asyncio.sleep(1)
