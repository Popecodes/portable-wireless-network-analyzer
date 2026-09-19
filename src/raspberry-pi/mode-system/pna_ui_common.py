#!/usr/bin/env python3
import time
from pathlib import Path
import lgpio
import spidev
from PIL import Image, ImageDraw, ImageFont

TFT_W, TFT_H = 480, 320
TFT_DC = 25
TFT_RST = 24
TFT_SPI_BUS = 0
TFT_SPI_DEV = 0
TFT_SPI_HZ = 32_000_000
TFT_MADCTL = 0xE8

LEFT_A, LEFT_B, LEFT_SW = 5, 6, 13
RIGHT_A, RIGHT_B, RIGHT_SW = 16, 20, 21

BG = (7,11,17)
PANEL = (18,27,39)
PANEL2 = (27,40,57)
TEXT = (236,242,248)
MUTED = (147,163,181)
ACCENT = (51,194,255)
GREEN = (62,220,150)
YELLOW = (250,204,21)
RED = (255,88,88)
PURPLE = (181,126,255)

def _font(size, bold=False):
    cands = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for p in cands:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

FONT_SM = _font(13)
FONT_BODY = _font(15)
FONT_MD = _font(18)
FONT_LG = _font(27, True)
FONT_TITLE = _font(17, True)

class ST7796S:
    def __init__(self):
        if not Path("/dev/spidev0.0").exists():
            raise RuntimeError("/dev/spidev0.0 not found")
        self.gpio = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(self.gpio, TFT_DC, 0)
        lgpio.gpio_claim_output(self.gpio, TFT_RST, 1)
        self.spi = spidev.SpiDev()
        self.spi.open(TFT_SPI_BUS, TFT_SPI_DEV)
        self.spi.mode = 0
        self.spi.max_speed_hz = TFT_SPI_HZ
        self.spi.lsbfirst = False
        self._init_panel()
        self._r565 = [(v & 0xF8) << 8 for v in range(256)]
        self._g565 = [(v & 0xFC) << 3 for v in range(256)]
        self._b565 = [v >> 3 for v in range(256)]

    def _dc(self, v): lgpio.gpio_write(self.gpio, TFT_DC, int(bool(v)))
    def _rst(self, v): lgpio.gpio_write(self.gpio, TFT_RST, int(bool(v)))
    def _write(self, data):
        if isinstance(data, int): data = bytes([data])
        self.spi.writebytes2(data)

    def command(self, cmd, data=b""):
        self._dc(0); self._write(bytes([cmd]))
        if data:
            self._dc(1); self._write(bytes(data))

    def _reset(self):
        self._rst(1); time.sleep(0.02)
        self._rst(0); time.sleep(0.12)
        self._rst(1); time.sleep(0.20)

    def _init_panel(self):
        self._reset()
        self.command(0x01); time.sleep(0.15)
        self.command(0x11); time.sleep(0.12)
        self.command(0xF0, [0xC3]); self.command(0xF0, [0x96])
        self.command(0x3A, [0x55])
        self.command(0x36, [TFT_MADCTL])
        self.command(0x20); self.command(0x13)
        self.command(0xF0, [0x3C]); self.command(0xF0, [0x69])
        self.command(0x29); time.sleep(0.05)

    def set_window(self, x0, y0, x1, y1):
        self.command(0x2A, [(x0>>8)&255, x0&255, (x1>>8)&255, x1&255])
        self.command(0x2B, [(y0>>8)&255, y0&255, (y1>>8)&255, y1&255])

    def show(self, image):
        image = image.convert("RGB")
        if image.size != (TFT_W, TFT_H):
            image = image.resize((TFT_W, TFT_H))
        raw = image.tobytes()
        out = bytearray(TFT_W*TFT_H*2)
        si = di = 0
        for _ in range(TFT_W*TFT_H):
            r,g,b = raw[si], raw[si+1], raw[si+2]
            value = self._r565[r] | self._g565[g] | self._b565[b]
            out[di] = (value >> 8) & 255
            out[di+1] = value & 255
            si += 3; di += 2
        self.set_window(0,0,TFT_W-1,TFT_H-1)
        self._dc(0); self._write(bytes([0x2C]))
        self._dc(1); self._write(out)

    def close(self):
        try: self.spi.close()
        except Exception: pass
        for pin in (TFT_DC, TFT_RST):
            try: lgpio.gpio_free(self.gpio, pin)
            except Exception: pass
        try: lgpio.gpiochip_close(self.gpio)
        except Exception: pass

class EncoderPair:
    TABLE = {
        (0,0,0,1):1,(0,1,1,1):1,(1,1,1,0):1,(1,0,0,0):1,
        (0,0,1,0):-1,(1,0,1,1):-1,(1,1,0,1):-1,(0,1,0,0):-1,
    }

    def __init__(self, steps_per_detent=4):
        self.h = lgpio.gpiochip_open(0)
        self.steps = steps_per_detent
        for pin in (LEFT_A,LEFT_B,LEFT_SW,RIGHT_A,RIGHT_B,RIGHT_SW):
            lgpio.gpio_claim_input(self.h, pin, lgpio.SET_PULL_UP)
        self.lp = self._pair(LEFT_A,LEFT_B)
        self.rp = self._pair(RIGHT_A,RIGHT_B)
        self.la = self.ra = 0
        self.lsw = self._read(LEFT_SW)
        self.rsw = self._read(RIGHT_SW)
        self.ldown = self.rdown = None
        self.lhold = self.rhold = False
        self.last_lp = self.last_rp = 0.0

    def _read(self,p): return lgpio.gpio_read(self.h,p)
    def _pair(self,a,b): return (self._read(a),self._read(b))

    def poll(self):
        ev=[]; now=time.monotonic()
        ln=self._pair(LEFT_A,LEFT_B)
        if ln != self.lp:
            self.la += self.TABLE.get(self.lp+ln,0); self.lp=ln
            if self.la >= self.steps: ev.append("LEFT_CW"); self.la=0
            elif self.la <= -self.steps: ev.append("LEFT_CCW"); self.la=0
        rn=self._pair(RIGHT_A,RIGHT_B)
        if rn != self.rp:
            self.ra += self.TABLE.get(self.rp+rn,0); self.rp=rn
            if self.ra >= self.steps: ev.append("RIGHT_CW"); self.ra=0
            elif self.ra <= -self.steps: ev.append("RIGHT_CCW"); self.ra=0

        l=self._read(LEFT_SW)
        if l != self.lsw:
            if l == 0:
                self.ldown=now; self.lhold=False
            else:
                if self.ldown is not None and not self.lhold and now-self.last_lp > 0.12:
                    ev.append("LEFT_PRESS"); self.last_lp=now
                self.ldown=None
            self.lsw=l

        r=self._read(RIGHT_SW)
        if r != self.rsw:
            if r == 0:
                self.rdown=now; self.rhold=False
            else:
                if self.rdown is not None and not self.rhold and now-self.last_rp > 0.12:
                    ev.append("RIGHT_PRESS"); self.last_rp=now
                self.rdown=None
            self.rsw=r

        if self.ldown is not None and not self.lhold and now-self.ldown >= 5:
            ev.append("LEFT_HOLD"); self.lhold=True
        if self.rdown is not None and not self.rhold and now-self.rdown >= 3:
            ev.append("RIGHT_HOLD"); self.rhold=True
        return ev

    def close(self):
        for pin in (LEFT_A,LEFT_B,LEFT_SW,RIGHT_A,RIGHT_B,RIGHT_SW):
            try: lgpio.gpio_free(self.h,pin)
            except Exception: pass
        try: lgpio.gpiochip_close(self.h)
        except Exception: pass

def new_frame():
    image = Image.new("RGB",(TFT_W,TFT_H),BG)
    return image, ImageDraw.Draw(image)

def text(draw, xy, value, fnt=None, fill=TEXT, anchor=None):
    draw.text(xy, str(value), font=fnt or FONT_BODY, fill=fill, anchor=anchor)

def panel(draw, box, fill=PANEL, outline=None, radius=9):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline)

def header(draw, title, right_text=""):
    draw.rectangle((0,0,TFT_W,36), fill=PANEL)
    text(draw,(12,18),"PNA V2",FONT_TITLE,ACCENT,anchor="lm")
    text(draw,(112,18),title,FONT_TITLE,TEXT,anchor="lm")
    if right_text:
        text(draw,(466,18),right_text,FONT_SM,MUTED,anchor="rm")

def footer(draw, message):
    draw.rectangle((0,TFT_H-25,TFT_W,TFT_H), fill=PANEL)
    text(draw,(12,TFT_H-12),message,FONT_SM,MUTED,anchor="lm")
