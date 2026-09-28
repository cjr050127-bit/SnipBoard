"""Synthetic 0.5.4/0.5.5 comparisons, without reading user pictures.

Report both the default-spacing change and the algorithm change at equal spacing.
Run with PYTHONPATH=src; writes only docs/packing-0.5.5, keeping past reports intact.
"""
import json
import math
import random
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from _packing_baseline_0_5_4 import compact as baseline_compact
from snipboard.packing import compact


OUTPUT = Path(__file__).resolve().parents[1] / 'docs' / 'packing-0.5.5'
CASES = [
    ('Mixed portrait / landscape', [.4,.6,.8,1,1.5,2,3,4],984,684),
    ('Panorama / portrait', [4,5,6,.3,.4,.5,1],984,684),
    ('13 square images', [1]*13,984,684),
    ('Portrait window', [.4,.6,.8,1,1.5,2,3,4],484,834),
    ('Wide window', [.4,.6,.8,1,1.5,2,3,4],1384,384),
    ('80 varied images', [random.Random(i).uniform(.3,3) for i in range(80)],984,684),
    ('240 varied images', [.5,1,2,3]*60,1200,800),
    ('Two portrait / landscape', [.6,1.6],984,684),
    ('Two images in portrait window', [.5,2],484,834),
]


def metrics(rows, positions, width, height):
    """Compute all metrics from displayed rectangles, independent of solver score."""
    areas = [positions[row['id']][2]*positions[row['id']][3]*row.get('fill',1.) for row in rows]
    mean = sum(areas)/len(areas)
    return dict(coverage=sum(areas)/(width*height),
                area_cv=math.sqrt(sum((area/mean-1)**2 for area in areas)/len(areas)),
                max_min_area_ratio=max(areas)/min(areas),
                min_relative_area=min(areas)/mean, max_relative_area=max(areas)/mean)


def verify(rows, positions, width, height, gap):
    """Reject misleading coverage from cropping, overlap or missing pictures."""
    assert positions is not None and set(positions)=={row['id'] for row in rows}
    for row in rows:
        x,y,w,h = positions[row['id']]
        assert all(math.isfinite(value) for value in (x,y,w,h))
        assert w>0 and h>0 and x>=-1e-6 and y>=-1e-6
        assert x+w<=width+1e-6 and y+h<=height+1e-6
        assert math.isclose(w/h,row['w']/row['h'],rel_tol=1e-8)
    values = list(positions.values())
    for index,(x,y,w,h) in enumerate(values):
        for a,b,c,d in values[index+1:]:
            assert x+w+gap<=a+1e-6 or a+c+gap<=x+1e-6 or y+h+gap<=b+1e-6 or b+d+gap<=y+1e-6


def draw_comparison(name, width, height, variants, path):
    image = Image.new('RGB',(2160,690),'#161a20')
    draw = ImageDraw.Draw(image)
    title = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',22)
    small = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',16)
    tiny = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',12)
    draw.text((24,14),name+' | Complete images; preserved aspect ratios',font=title,fill='white')
    colors = ['#67b4b0','#e7aa73','#829ccb','#b995c9','#94b378','#cb858c','#b9ae72','#72a8c8']
    for panel,(label,positions,record) in enumerate(variants):
        left,top = 24+720*panel,142
        scale = min(672/width,490/height)
        draw.text((left,54),label,font=title,fill='white')
        draw.text((left,86),f"Coverage {record['coverage']:.1%} | Area CV {record['area_cv']:.2f}",font=small,fill='#d8dce3')
        draw.text((left,111),f"Largest / smallest area {record['max_min_area_ratio']:.2f}x",font=small,fill='#d8dce3')
        draw.rectangle((left,top,left+width*scale,top+height*scale),fill='#060809',outline='#697582')
        for key,(x,y,w,h) in positions.items():
            draw.rectangle((left+x*scale,top+y*scale,left+(x+w)*scale,top+(y+h)*scale),fill=colors[int(key)%len(colors)])
            if min(w,h)*scale>18:
                draw.text((left+x*scale+3,top+y*scale+1),key,font=tiny,fill='#17212b')
    draw.text((24,657),'Lower area CV and largest/smallest ratio mean more balanced displayed image areas. Synthetic rectangles only.',font=small,fill='#9aa8b9')
    image.save(path)


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    records = []
    for index,(name,ratios,width,height) in enumerate(CASES):
        rows = [dict(id=str(i),w=260.,h=260/ratio,name=str(i),path='',order=i) for i,ratio in enumerate(ratios)]
        variants = []
        record = dict(name=name,count=len(rows),width=width,height=height)
        for key,label,solver,gap in [
            ('baseline_default','0.5.4 default: 18 px',baseline_compact,18),
            ('baseline_equal_gap','0.5.4 at equal gap: 3 px',baseline_compact,3),
            ('current_default','0.5.5 default: 3 px',compact,3),
        ]:
            started = time.monotonic()
            result = solver(rows,width,height,gap)
            elapsed = time.monotonic()-started
            verify(rows,result['positions'],width,height,gap)
            values = dict(gap=gap,seconds=elapsed,method=result['method'],**metrics(rows,result['positions'],width,height))
            record[key] = values
            variants.append((label,result['positions'],values))
        records.append(record)
        if index<3 or len(rows) in (2,80):
            draw_comparison(name,width,height,variants,OUTPUT/f'comparison-{index+1}.png')
        print(json.dumps(record,ensure_ascii=False),flush=True)
    report = dict(versions=['0.5.4','0.5.5'],
                  note='Synthetic aspect ratios; measured logical viewport pixels. Coverage is full image area / viewport area. Time-bounded solvers may choose different candidates on different machines.',
                  cases=records)
    (OUTPUT/'benchmark.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
