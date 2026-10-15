"""
Cloud Reviewer (cloud_reviewer.py)
Connects to NVIDIA Nemotron via Nebius Token Factory with local Ollama fallback.
"""

import os
from openai import OpenAI

class CloudReviewer:
    def __init__(self):
        # Nebius Token Factory / NVIDIA Nemotron configuration
        # Make sure your NEBIUS_API_KEY environment variable is set, or paste key directly here for testing
        self.api_key = os.getenv("NEBIUS_API_KEY", "YOUR_NEBIUS_API_KEY_HERE")
        self.base_url = "https://api.studio.nebius.ai/v1/"  # Nebius endpoint
        self.model_name = "nvidia/nemotron-4-34b-instruct" # Standard Nemotron endpoint model name

        try:
            self.client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key
            )
        except Exception as e:
            print(f"[CloudReviewer Warning]: Failed to initialize Nebius client: {e}")
            self.client = None

    def get_design_explanation(self, prompt: str) -> str:
        """
        Sends prompt to NVIDIA Nemotron via Nebius Token Factory.
        Falls back to local Ollama if cloud connection fails.
        """
        # Try Cloud (Nebius / Nemotron)
        if self.client and self.api_key != "YOUR_NEBIUS_API_KEY_HERE":
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": "You are CircuitProof, an expert hardware design agent."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.2,
                    max_tokens=1024
                ]
                return response.choices[0].message.content
            except Exception as e:
                print(f"[CloudReviewer]: Nebius cloud call failed ({e}). Falling back to local Ollama...")

        # Fallback to Local Ollama (nemotron or llama3) if cloud key isn't set or fails
        try:
            import ollama
            response = ollama.chat(model='llama3', messages=[
                {"role": "user", "content": prompt}
            ])
            return response['message']['content']
        except Exception as local_err:
            return f"[Error]: Both cloud (Nebius) and local (Ollama) failed. Details: {local_err}"