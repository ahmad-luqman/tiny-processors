"""G1: literal coverage, snapshot oracle, native C and byte-port RTL."""
import ctypes as C
import os
from pathlib import Path
import random
import subprocess
import unittest
ROOT=Path(__file__).resolve().parents[1]
BUILD=ROOT/'build/gfx'
U32=C.c_uint32

def command(op=1,**kw):
    names='op color x0 y0 x1 y1 x2 y2 w h src stride sw sh sx sy'.split()
    p=[0]*16;p[0]=op;p[1]=0xa6
    for k,v in kw.items():p[names.index(k)]=v
    return p

def oracle(fb,p):
    op,col,x0,y0,x1,y1,x2,y2,w,h,src,stride,sw,sh,sx,sy=p
    def put(x,y,v):
        if 0<=x<320 and 0<=y<240:fb[y*320+x]=v
    if op in (1,2):
        source=bytes(fb) if src==0x30000000 else bytes((i*17+3)&255 for i in range(262144)) if op==2 else b''
        base=0 if src==0x30000000 else src-0x80000000
        for j in range(h):
            for i in range(w):
                if op==1:put(x0+i,y0+j,col)
                elif 0<=sx+i<sw and 0<=sy+j<sh:put(x0+i,y0+j,source[base+(sy+j)*stride+sx+i])
    elif op==3:
        if (x0,y0)>(x1,y1):x0,y0,x1,y1=x1,y1,x0,y0
        # Independent major-axis closed form, with ties toward the endpoint.
        dx=x1-x0;dy=abs(y1-y0);sign=1 if y1>=y0 else -1
        if dx>=dy:
            for i in range(dx+1):put(x0+i,y0+sign*((2*i*dy+dx)//(2*dx) if dx else 0),col)
        else:
            for i in range(dy+1):put(x0+(2*i*dx+dy)//(2*dy),y0+sign*i,col)
    else:
        v=[(x0,y0),(x1,y1),(x2,y2)]
        area=(x1-x0)*(y2-y0)-(y1-y0)*(x2-x0)
        if not area:return
        if area<0:v[1],v[2]=v[2],v[1]
        for y in range(max(0,min(t[1] for t in v)),min(240,max(t[1] for t in v))):
            for x in range(max(0,min(t[0] for t in v)),min(320,max(t[0] for t in v))):
                ok=True
                for a,b in zip(v,v[1:]+v[:1]):
                    e=(b[0]-a[0])*(2*y+1-2*a[1])-(b[1]-a[1])*(2*x+1-2*a[0])
                    ok &= e>0 or e==0 and (b[1]<a[1] or b[1]==a[1] and b[0]>a[0])
                if ok:put(x,y,col)

def fnv(data):
    h=2166136261
    for b in data:h=((h^b)*16777619)&0xffffffff
    return h

class Graphics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        BUILD.mkdir(parents=True,exist_ok=True)
        subprocess.run([os.environ.get('HOST_CC','cc'),'-shared','-fPIC','-O2','-std=c11','-Wall','-Wextra','-Werror','-o',str(BUILD/'native.dylib'),'tests/rv32_gpu_native.c','tools/rv32_gpu.c','programs/rv32/gpu_ref.c','programs/rv32/gfx.c'],cwd=ROOT,check=True)
        cls.lib=C.CDLL(str(BUILD/'native.dylib'))
        cls.lib.native_gpu_fb.restype=C.POINTER(C.c_uint8)
        cls.lib.native_gpu_ram.restype=C.POINTER(C.c_uint8)
        cls.lib.native_gpu_access.argtypes=[U32,C.c_int,C.c_int,C.POINTER(U32)]
        cls.lib.native_gpu_lock.argtypes=[U32,C.c_int]
        cls.lib.gpu_reference.argtypes=[C.POINTER(C.c_uint8),C.POINTER(C.c_uint8),C.POINTER(U32)]
    def access(self,off,v=None,width=4,ok=True):
        value=U32(v or 0)
        self.assertEqual(bool(self.lib.native_gpu_access(off,width,v is not None,C.byref(value))),ok)
        return value.value
    def submit(self,p):
        for i,v in enumerate(p):self.access(64+4*i,v&0xffffffff)
        self.access(0,1);self.lib.native_gpu_tick(0) # command tick
    def run_command(self,p,mode=0,abort=-1):
        self.submit(p);ticks=0
        while self.access(4)==1:
            if ticks==abort:
                if mode&2:self.lib.native_gpu_reset()
                else:self.access(0,2)
            self.lib.native_gpu_tick(mode&1 and ticks%7<2);ticks+=1
            self.assertLess(ticks,4000000)
        return [fnv(C.string_at(self.lib.native_gpu_fb(),76800))]+[self.access(o) for o in (4,8,12,16,20,24)]
    def test_pixels_and_rtl(self):
        self.lib.native_gpu_init();expected=bytearray((i*13+7)&255 for i in range(76800))
        cmds=[command(w=0,h=4),command(x0=-3,y0=-2,w=6,h=5),command(x0=319,y0=239,w=5,h=8),command(x0=-1024,y0=1023,w=2048,h=0)]
        for x,y in [(8,3),(3,8),(-8,3),(-3,8),(8,-3),(3,-8),(-8,-3),(-3,-8),(0,0),(0,8),(8,0),(2,1)]:
            cmds += [command(3,x0=4,y0=4,x1=4+x,y1=4+y),command(3,x1=4,y1=4,x0=4+x,y0=4+y)]
        cmds += [command(4,x0=1,y0=1,x1=8,y1=1,x2=1,y2=8),command(4,x0=8,y0=8,x1=1,y1=8,x2=8,y2=1),command(4,x0=-1024,y0=-1024,x1=1023,y1=1023,x2=0,y2=0)]
        for dx,dy in [(1,0),(-1,0),(0,1),(0,-1),(1,1),(-1,-1),(0,0),(-20,-20)]:
            cmds.append(command(2,x0=10+dx,y0=10+dy,w=30,h=20,src=0x30000000,stride=320,sw=320,sh=240,sx=10,sy=10))
        cmds += [command(2,x0=-4,y0=4,w=20,h=20,src=0x80000003,stride=19,sw=17,sh=15,sx=-2,sy=-1)]
        cmds += [command(x0=x,y0=y,w=2,h=2) for x,y in [(-1,-1),(319,-1),(-1,239),(319,239)]]
        cmds += [command(3,x0=-1024,y0=-1024,x1=1023,y1=1023),command(3,x0=-1024,y0=1023,x1=1023,y1=-1024),
                 command(2,x0=319,y0=239,w=1,h=1,src=0x8003ffff,stride=1,sw=1,sh=1)]
        rng=random.Random(20260922)
        for _ in range(60):
            op=rng.choice([1,3,4]);kw={k:rng.randrange(-20,80) for k in ('x0','y0','x1','y1','x2','y2')}
            cmds.append(command(op,**kw,w=rng.randrange(50),h=rng.randrange(50),color=rng.randrange(256)))
        for _ in range(24):
            cmds.append(command(2,x0=rng.choice([-8,0,310]),y0=rng.choice([-6,0,230]),w=20,h=20,
                src=0x80000003,stride=19,sw=17,sh=15,sx=rng.randrange(-5,20),sy=rng.randrange(-5,20)))
        records=[];rows=[]
        for n,p in enumerate(cmds):
            before=bytes(expected);oracle(expected,p)
            ref=(C.c_uint8*76800).from_buffer_copy(before)
            src=ref if p[10]==0x30000000 else C.cast(C.byref(self.lib.native_gpu_ram().contents,max(0,p[10]-0x80000000)),C.POINTER(C.c_uint8))
            self.lib.gpu_reference(ref,src,(U32*16)(*(v&0xffffffff for v in p)))
            self.assertEqual(bytes(ref),bytes(expected),('reference',n,p))
            rec=self.run_command(p,n%2)
            self.assertEqual(C.string_at(self.lib.native_gpu_fb(),76800),bytes(expected),('device',n,p))
            self.assertEqual(rec[1:3],[2,0]);records.append(rec);rows.append((n%2,-1,p))
        # Invalid geometry/source and reset through setup, scan, held read/write, advance.
        for p in [command(9),command(x0=1024,w=1,h=1),command(2,w=2,h=2,src=0xfffffff0,stride=32,sw=32,sh=2),command(color=256),command(w=2049),command(h=2049),command(2,w=1,h=1,src=0x8003ffff,stride=2,sw=2,sh=1),command(2,w=1,h=1,src=0x30000001,stride=320,sw=320,sh=240),command(2,sw=0,sh=1),command(2,sw=1,sh=0),command(2,sw=2,sh=2,stride=1),command(3,x1=1024),command(4,y2=-1025)]:
            rec=self.run_command(p);self.assertEqual(rec[1:3],[4,1]);records.append(rec);rows.append((0,-1,p))
        valid_blit=command(2,w=2,h=2,src=0x80000000,stride=16,sw=16,sh=16)
        for field,value in [(11,15),(12,0),(13,0),(14,1024),(15,-1025)]:
            p=valid_blit.copy();p[field]=value
            rec=self.run_command(p);self.assertEqual(rec[1:3],[4,1]);records.append(rec);rows.append((0,-1,p))
        p=command(2,w=2,h=2,src=0x30000000,stride=320,sw=319,sh=240)
        rec=self.run_command(p);self.assertEqual(rec[1:3],[4,1]);records.append(rec);rows.append((0,-1,p))
        for mode in (1,3):
            for tick in range(12):
                p=command(2,x0=10,y0=10,w=10,h=10,src=0x80000000+tick*17,stride=16,sw=16,sh=16)
                rec=self.run_command(p,mode,tick);self.assertEqual(rec[1:],[0]*6);records.append(rec);rows.append((mode,tick,p))
        p=command(w=3,h=2);records.append(self.run_command(p));rows.append((0,-1,p))
        fixture=BUILD/'commands.txt';fixture.write_text(''.join(f'{m} {a} '+ ' '.join(f'{v&0xffffffff:08x}' for v in p)+'\n' for m,a,p in rows))
        sim=os.environ.get('G1_SIM','icarus')
        if sim=='icarus':
            subprocess.run(['iverilog','-g2012','-s','rv32_gpu_tb','-o',str(BUILD/'gpu.vvp'),'tests/rv32_gpu_tb.sv','rtl/rv32/rv32_gpu.v'],cwd=ROOT,check=True)
            run=['vvp',str(BUILD/'gpu.vvp')]
        elif sim=='verilator':
            log=(BUILD/'verilator-build.log').open('w')
            subprocess.run(['verilator','--binary','--timing','--trace','--top-module','rv32_gpu_tb','--Mdir',str(BUILD/'verilator'),'-o','gpu_sim','tests/rv32_gpu_tb.sv','rtl/rv32/rv32_gpu.v'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
            log.close()
            run=[str(BUILD/'verilator/gpu_sim')]
        else:self.fail('unknown G1_SIM')
        result=subprocess.run(run+[f'+input={fixture}'],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        (BUILD/f'{sim}.log').write_text(result.stdout)
        lines=[l.split() for l in result.stdout.splitlines() if l.startswith('RESULT ')]
        self.assertEqual(len(lines),len(records));self.assertIn(f'PASS {len(records)}',result.stdout)
        for n,(line,want) in enumerate(zip(lines,records,strict=True)):
            self.assertEqual([int(v,16) for v in line[2:]],want,(n,rows[n]))
        for bad in ('', '0 -1 '+ '00000000 '*15, '0 -1 '+ '100000001 '+'00000000 '*15, '0 -1 '+ 'xxxxxxxx '+'00000000 '*15):
            invalid=BUILD/'invalid.txt';invalid.write_text(bad)
            rejected=subprocess.run(run+[f'+input={invalid}'],text=True,capture_output=True)
            self.assertNotEqual(rejected.returncode,0)
            self.assertNotIn('PASS ',rejected.stdout)

    def test_literal_triangle_partition(self):
        self.assertEqual(self.lib.native_gpu_anchor(),0)
    def test_mmio_ownership(self):
        self.lib.native_gpu_init()
        for off in (0,28,32,60,128):self.access(off,ok=False)
        for width in (1,2):self.access(4,width=width,ok=False)
        self.access(4,0,ok=False);self.access(0,3,ok=False)
        p=command(2,w=10,h=10,src=0x80001003,stride=20,sw=10,sh=10)
        self.submit(p)
        self.access(0,1,ok=False);self.access(64,1,ok=False)
        self.assertEqual(self.access(64),2)
        for addr,width,want in [(0x80001000,4,1),(0x80001000,1,0),(0x800010c0,1,1),(0x800010c1,1,0)]:
            self.assertEqual(self.lib.native_gpu_lock(addr,width),want)
        self.access(0,2);self.assertEqual(self.access(4),0);self.assertEqual(self.access(64),0)
        for p in (command(2,w=1,h=1,src=0x80001000,stride=16,sw=16,sh=0),
                  command(2,w=1,h=1,src=0xfffffff0,stride=16,sw=16,sh=2)):
            self.submit(p)
            self.assertEqual(self.lib.native_gpu_lock(p[10],1),0)
            self.lib.native_gpu_tick(0)
            self.assertEqual(self.access(4),4)

if __name__=='__main__':unittest.main()
