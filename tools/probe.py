"""Phase 0 probe: list clips / tagged layers / instances in the open TVPaint project."""
import re, sys, time
from pytvpaint import george
from pytvpaint.project import Project

TAG = re.compile(r"_(сс|cc)-", re.IGNORECASE)
t = time.time()
p = Project.current_project()
print("project:", p.name, p.path, p.width, "x", p.height, "fps", p.fps, "start_frame", p.start_frame)
for clip in p.clips:
    names = list(clip.layer_names)
    print(f"clip {clip.name!r} id={clip.id} start={clip.start} end={clip.end} layers={len(names)}")
    if clip.name != "TEST":
        continue
    for layer in clip.layers:
        tagged = bool(TAG.search(layer.name))
        print(f"  layer pos={layer.position} id={layer.id} name={layer.name!r} type={layer.layer_type} anim={layer.is_anim_layer} start={layer.start} end={layer.end} tagged={tagged}")
        if tagged:
            for inst in layer.instances:
                print(f"     inst start={inst.start} len={inst.length} name={inst.name!r}")
print("elapsed", round(time.time()-t, 2))
