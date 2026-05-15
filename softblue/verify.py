"""FFT verification of generated sequences (numpy-only)."""

from __future__ import annotations

import numpy as np


class ToneVerifier:
    """Window a signal into chunks and report dominant frequencies per chunk."""

    def verify_sequence(
        self, samples: np.ndarray, sample_rate: int = 8000, chunk_ms: int = 100
    ) -> list[dict]:
        chunk_size = max(1, int(sample_rate * chunk_ms / 1000))
        results = []
        for i in range(0, len(samples), chunk_size):
            chunk = samples[i : i + chunk_size]
            if len(chunk) < chunk_size:
                continue
            windowed = chunk * np.hanning(len(chunk))
            spectrum = np.abs(np.fft.rfft(windowed))
            freqs = np.fft.rfftfreq(len(chunk), 1 / sample_rate)
            peaks = self._find_peaks(freqs, spectrum)
            results.append(
                {
                    "time": round(i / sample_rate, 3),
                    "frequencies": peaks,
                    "silent": float(np.max(np.abs(chunk))) < 1e-3,
                }
            )
        return results

    @staticmethod
    def _find_peaks(freqs, spectrum, threshold: float = 0.25) -> list[dict]:
        max_power = float(np.max(spectrum)) or 1.0
        peaks = []
        for i in range(1, len(spectrum) - 1):
            if (
                spectrum[i] > spectrum[i - 1]
                and spectrum[i] > spectrum[i + 1]
                and spectrum[i] > max_power * threshold
            ):
                peaks.append(
                    {
                        "frequency": round(float(freqs[i])),
                        "power": round(float(spectrum[i] / max_power), 3),
                    }
                )
        return sorted(peaks, key=lambda p: -p["power"])[:4]
