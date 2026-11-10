"""Local bilingual phoneme keyword spotting. No network or API credential."""
from pathlib import Path


class TarsWakeWord:
    def __init__(self, directory):
        import sherpa_onnx
        self.np = __import__('numpy')
        root = Path(directory)
        stem = 'epoch-13-avg-2-chunk-16-left-64'
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(root/'tokens.txt'), encoder=str(root/f'encoder-{stem}.int8.onnx'),
            decoder=str(root/f'decoder-{stem}.onnx'), joiner=str(root/f'joiner-{stem}.int8.onnx'),
            keywords_file=str(Path(__file__).with_name('tars_keywords.txt')),
            num_threads=2, keywords_score=2.0, keywords_threshold=.2, num_trailing_blanks=1)
        self.reset()

    def reset(self):
        self.stream = self.spotter.create_stream()
        self.stream.accept_waveform(16000, self.np.zeros(8000, dtype=self.np.float32))
        while self.spotter.is_ready(self.stream): self.spotter.decode_stream(self.stream)

    def feed(self, pcm):
        values = self.np.frombuffer(pcm, dtype='<i2').astype(self.np.float32) / 32768.
        self.stream.accept_waveform(16000, values)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
            result = self.spotter.get_result(self.stream)
            if result:
                self.spotter.reset_stream(self.stream)
                return result
        return ''
