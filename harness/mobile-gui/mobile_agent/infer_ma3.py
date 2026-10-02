# Copyright 2024 The android_world Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Some LLM inference interface."""

import abc
import time
from typing import Any, Optional
import numpy as np
from PIL import Image
from openai import OpenAI
from qwen_vl_utils import smart_resize
from io import BytesIO
import base64
import json
from pathlib import Path
from collections.abc import Callable

ERROR_CALLING_LLM = 'Error calling LLM'


class LlmResponseError(RuntimeError):
    """No usable response was returned within the inference retry limit."""

def pil_to_base64(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG") 
    return base64.b64encode(buffer.getvalue()).decode("utf-8")

def image_to_base64(image_path):
  dummy_image = Image.open(image_path)
  MIN_PIXELS=3136
  MAX_PIXELS=10035200
  resized_height, resized_width  = smart_resize(dummy_image.height,
      dummy_image.width,
      factor=28,
      min_pixels=MIN_PIXELS,
      max_pixels=MAX_PIXELS,)
  dummy_image = dummy_image.resize((resized_width, resized_height))
  return f"data:image/png;base64,{pil_to_base64(dummy_image)}"

class LlmWrapper(abc.ABC):
  """Abstract interface for (text only) LLM."""

  @abc.abstractmethod
  def predict(
      self,
      text_prompt: str,
  ) -> tuple[str, Optional[bool], Any]:
    """Calling multimodal LLM with a prompt and a list of images.

    Args:
      text_prompt: Text prompt.

    Returns:
      Text output, is_safe, and raw output.
    """

class MultimodalLlmWrapper(abc.ABC):
  """Abstract interface for Multimodal LLM."""

  @abc.abstractmethod
  def predict_mm(
      self, text_prompt: str, images: list[np.ndarray], messages = None
  ) -> tuple[str, Optional[bool], Any]:
    """Calling multimodal LLM with a prompt and a list of images.

    Args:
      text_prompt: Text prompt.
      images: List of images as numpy ndarray.

    Returns:
      Text output and raw output.
    """

class GUIOwlWrapper(LlmWrapper, MultimodalLlmWrapper):

    RETRY_WAITING_SECONDS = 20

    def __init__(
            self,
            api_key: str,
            base_url: str,
            model_name: str,
            max_retry: int = 10,
            temperature: float = 0.0,
    ):
        if max_retry <= 0:
            max_retry = 10
            print('Max_retry must be positive. Reset it to 3')
        self.max_retry = min(max_retry, 10)
        self.temperature = temperature
        self.model = model_name
        self.response_log_path = None
        self.bot = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=30
        )

    def convert_messages_format_to_openaiurl(self, messages):
      converted_messages = []
      for message in messages:
          new_content = []
          for item in message['content']:
              if list(item.keys())[0] == 'text':
                  new_content.append({'type': 'text', 'text': item['text']})
              elif list(item.keys())[0] == 'image':
                new_content.append({'type': 'image_url', 'image_url': {'url': image_to_base64(item['image'])}})
          converted_messages.append({'role': message['role'], 'content': new_content})

      return converted_messages
    
    def predict(
            self,
            text_prompt: str,
    ) -> tuple[str, Optional[bool], Any]:
        return self.predict_mm(text_prompt, [])

    def predict_mm(
            self, text_prompt: str, images: list[np.ndarray], messages = None,
            response_validator: Optional[Callable[[str], bool]] = None,
    ) -> tuple[str, Optional[bool], Any]:
        
        if messages is None:
          payload = [
              {
                  "role": "user",
                  "content": [
                      {"text": text_prompt},
                  ]
              }
          ]
          
          for image in images:
            payload[0]['content'].append({
                'image': image
            })
          
          payload = self.convert_messages_format_to_openaiurl(payload)
        else:
          payload = messages
        
        last_error = None
        for attempt in range(1, self.max_retry + 1):
            response = None
            try:
                response = self.bot.chat.completions.create(model=self.model, messages=payload)
                if not response.choices:
                    raise LlmResponseError('Provider returned no completion choices')
                choice = response.choices[0]
                if choice.finish_reason != 'stop':
                    raise LlmResponseError(
                        f'Incomplete or unexpected response: finish_reason={choice.finish_reason!r}'
                    )
                content = choice.message.content
                if not isinstance(content, str) or not content.strip():
                    raise LlmResponseError('Provider returned empty message.content')
                if response_validator is not None and not response_validator(content):
                    raise LlmResponseError('Model answer does not match the required response format')
            except Exception as e:
                last_error = e
                self._record_response(attempt, response, str(e))
                print(f'LLM attempt {attempt}/{self.max_retry} failed: {e}')
                if attempt < self.max_retry:
                    time.sleep(self.RETRY_WAITING_SECONDS)
            else:
                self._record_response(attempt, response, None)
                return content, payload, response
        raise LlmResponseError(
            f'No usable model response after {self.max_retry} attempts: {last_error}'
        ) from last_error

    def _record_response(self, attempt, response, error):
        if self.response_log_path is None:
            return
        path = Path(self.response_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            'attempt': attempt,
            'error': error,
            'response': response.model_dump(mode='json') if response is not None else None,
        }
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
