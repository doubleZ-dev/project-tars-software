"""Explicit, bounded libopus binding; private DLL source is pinned in native/."""
import ctypes as c
import os
from pathlib import Path

def library():
    if os.name=='nt': path=str(Path(__file__).with_name('native')/'opus.dll')
    else:
        from ctypes.util import find_library
        path=find_library('opus')
    lib=c.CDLL(path)
    lib.opus_encoder_create.argtypes=[c.c_int,c.c_int,c.c_int,c.POINTER(c.c_int)]; lib.opus_encoder_create.restype=c.c_void_p
    lib.opus_decoder_create.argtypes=[c.c_int,c.c_int,c.POINTER(c.c_int)]; lib.opus_decoder_create.restype=c.c_void_p
    lib.opus_encode.argtypes=[c.c_void_p,c.c_void_p,c.c_int,c.c_void_p,c.c_int]; lib.opus_encode.restype=c.c_int
    lib.opus_decode.argtypes=[c.c_void_p,c.c_void_p,c.c_int,c.c_void_p,c.c_int,c.c_int]; lib.opus_decode.restype=c.c_int
    lib.opus_encoder_destroy.argtypes=[c.c_void_p]; lib.opus_decoder_destroy.argtypes=[c.c_void_p]
    return lib

class Opus:
    samples=960  # 60ms, 16kHz, mono, matches negotiated Xiaozhi parameters
    def __init__(self):
        self.lib=library(); err=c.c_int()
        self.encoder=self.lib.opus_encoder_create(16000,1,2048,c.byref(err))
        if not self.encoder or err.value: raise RuntimeError('opus_encoder_init')
        self.decoder=self.lib.opus_decoder_create(16000,1,c.byref(err))
        if not self.decoder or err.value:
            self.lib.opus_encoder_destroy(self.encoder); self.encoder=None
            raise RuntimeError('opus_decoder_init')
    def encode(self,pcm):
        if len(pcm)!=1920: raise ValueError('opus_frame_requires_60ms_pcm')
        output=c.create_string_buffer(1275)
        n=self.lib.opus_encode(self.encoder,pcm,960,output,1275)
        if n<0: raise ValueError('opus_encode_failed')
        return output.raw[:n]
    def decode(self,packet):
        if not 0<len(packet)<=1275: raise ValueError('opus_packet_size')
        output=c.create_string_buffer(1920)
        n=self.lib.opus_decode(self.decoder,packet,len(packet),output,960,0)
        if n!=960: raise ValueError('opus_packet_duration')
        return output.raw[:n*2]
    def close(self):
        if self.encoder: self.lib.opus_encoder_destroy(self.encoder); self.encoder=None
        if self.decoder: self.lib.opus_decoder_destroy(self.decoder); self.decoder=None

