"""Appearance state survives browser lifecycle without runtime/account changes."""
import re
import subprocess
from pathlib import Path

STATIC = Path(__file__).parents[1] / "src" / "glass_drive_ui" / "static"


def test_system_manual_and_cross_tab_appearance_recover_without_storage():
    script = r"""
      const fs = require('node:fs'); const vm = require('node:vm');
      const assert = require('node:assert/strict');
      const source = fs.readFileSync(process.argv[1], 'utf8');
      const key = 'xperfect.appearance';
      function page({saved, dark=false, blocked=false, missing=false}={}) {
        const values = new Map(saved === undefined ? [] : [[key,saved]]);
        const events = {}, documentEvents = {}, mediaEvents = {}, writes = [];
        const pickers = [0,1].flatMap(group=>['light','system','dark'].map(value=>({group,value,checked:false,events:{},addEventListener(type,fn){this.events[type]=fn;}})));
        const media = {matches:dark,addEventListener(type,fn){mediaEvents[type]=fn;}};
        const root = {dataset:{}};
        const context = {Set,window:{matchMedia:()=>media,addEventListener(type,fn){events[type]=fn;}},
          document:{documentElement:root,getElementById:()=>null,querySelectorAll:()=>pickers,addEventListener(type,fn){documentEvents[type]=fn;}}};
        if(!missing) context.localStorage = {
          getItem(name){if(blocked) throw Error('Storage blocked');return values.get(name)??null;},
          setItem(name,value){if(blocked) throw Error('Storage blocked');values.set(name,value);writes.push([name,value]);}
        };
        vm.runInNewContext(source,context);
        const initial = root.dataset.theme;
        documentEvents.DOMContentLoaded();
        return {root,pickers,values,writes,initial,
          choose(value){const picker=pickers.find(p=>p.group===0&&p.value===value);pickers.filter(p=>p.group===0).forEach(p=>p.checked=false);picker.checked=true;picker.events.change();},
          uncheckEvent(value){pickers.find(p=>p.group===0&&p.value===value).events.change();},
          os(dark){media.matches=dark;mediaEvents.change();},
          storage(event){events.storage(event);}};
      }
      const system=page();
      assert.equal(system.initial,'light','apply before DOMContentLoaded to avoid wrong-theme first paint');
      assert(system.pickers.every(p=>p.checked===(p.value==='system')));
      system.os(true);assert.equal(system.root.dataset.theme,'dark');
      system.os(false);assert.equal(system.root.dataset.theme,'light');
      assert.equal(system.writes.length,0,'OS follow must not create a manual override');

      system.choose('dark');assert.equal(system.root.dataset.theme,'dark');
      assert.equal(system.values.get(key),'dark');
      assert(system.pickers.every(p=>p.checked===(p.value==='dark')),'same-page appearance controls stay in sync');
      system.uncheckEvent('light');assert.equal(system.values.get(key),'dark','unselected radio changes cannot overwrite the chosen appearance');
      system.os(false);assert.equal(system.root.dataset.theme,'dark','manual choice overrides OS');
      const restored=page({saved:system.values.get(key),dark:false});
      assert.equal(restored.initial,'dark','manual choice survives a page reload');
      restored.choose('system');restored.os(true);
      assert.equal(restored.root.dataset.theme,'dark');assert.equal(restored.values.get(key),'system');

      const shared=page({saved:'light',dark:true});
      shared.storage({key:'unrelated.setting',newValue:'dark'});assert.equal(shared.root.dataset.theme,'light');
      shared.storage({key,newValue:'dark'});assert.equal(shared.root.dataset.theme,'dark');
      shared.storage({key,newValue:'not-a-theme'});assert(shared.pickers.every(p=>p.checked===(p.value==='system')));
      shared.os(false);assert.equal(shared.root.dataset.theme,'light');
      shared.storage({key,newValue:'dark'});shared.storage({key:null,newValue:null});
      assert.equal(shared.root.dataset.theme,'light','storage clear restores current system preference');
      shared.os(true);assert.equal(shared.root.dataset.theme,'dark','cleared state resumes OS following');

      const unknown=page({saved:'invalid-value',dark:true});
      assert.equal(unknown.initial,'dark');assert(unknown.pickers.every(p=>p.checked===(p.value==='system')));
      for(const options of [{blocked:true},{missing:true}]) {
        const isolated=page({...options,dark:false});
        assert.equal(isolated.initial,'light');isolated.choose('dark');isolated.os(false);
        assert.equal(isolated.root.dataset.theme,'dark','storage failure must not break local manual choice');
        isolated.choose('system');isolated.os(true);assert.equal(isolated.root.dataset.theme,'dark');
      }
      console.log('appearance lifecycle causal checks passed');
    """
    result = subprocess.run(["node", "--eval", script, str(STATIC / "theme.js")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "appearance lifecycle causal checks passed" in result.stdout


def _luminance(hex_color):
    values = [int(hex_color[index:index+2], 16) / 255 for index in (1, 3, 5)]
    values = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
              for value in values]
    return sum(value * weight for value, weight in zip(values, (.2126, .7152, .0722)))


def _contrast(first, second):
    dark, light = sorted((_luminance(first), _luminance(second)))
    return (light + .05) / (dark + .05)


def test_actual_theme_critical_solid_colour_pairs_remain_readable():
    css = (STATIC / "theme.css").read_text()
    tokens = {name: (light.lower(), dark.lower()) for name, light, dark in re.findall(
        r"(--[a-z-]+):\s*light-dark\((#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)", css)}
    for mode in (0, 1):
        for foreground in ("--text", "--muted", "--placeholder", "--accent", "--queue", "--danger", "--warning", "--success"):
            for background in ("--bg", "--panel", "--panel-strong", "--overlay-surface", "--control-bg", "--control-hover", "--surface-hover", "--segment-selected"):
                ratio = _contrast(tokens[foreground][mode], tokens[background][mode])
                assert ratio >= 4.5, (mode, foreground, background, round(ratio, 2))
        # Boundaries used to identify ordinary fields and focus remain visible.
        for background in ("--panel", "--panel-strong", "--overlay-surface", "--control-bg"):
            assert _contrast(tokens["--control-line"][mode], tokens[background][mode]) >= 3
            assert _contrast(tokens["--accent"][mode], tokens[background][mode]) >= 3
        assert _contrast(tokens["--button-text"][mode], tokens["--button-bg"][mode]) >= 4.5
        assert _contrast(tokens["--on-accent"][mode], tokens["--accent"][mode]) >= 4.5
        assert _contrast(tokens["--text"][mode], tokens["--segment-selected"][mode]) >= 4.5

    # Loading/recovery copy belongs to app chrome, not the remote desktop canvas.
    consumer_css = (STATIC / "styles.css").read_text()
    def declarations(selector):
        result = {}
        for prelude, block in re.findall(r"([^{}]+)\{([^{}]+)\}", consumer_css):
            prelude = re.sub(r"/\*.*?\*/", "", prelude, flags=re.DOTALL).strip()
            if selector in [item.strip() for item in prelude.split(",")]:
                result.update(re.findall(r"([\w-]+)\s*:\s*([^;]+)", block))
        return result

    def colour(value, mode):
        variable = re.fullmatch(r"var\((--[a-z-]+)\)", value.strip())
        if variable:
            return tokens[variable[1]][mode]
        value = value.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{3}", value):
            return "#" + "".join(character * 2 for character in value[1:])
        return value

    overlay = declarations(".stage-overlay")
    sign_in = declarations(".watch-sign-in:not([hidden])")
    for mode in (0, 1):
        assert _contrast(tokens["--text"][mode], colour(overlay["background"], mode)) >= 4.5, \
            "Watch loading/recovery text must be readable outside the native desktop"
        assert _contrast(colour(sign_in["color"], mode), colour(sign_in["background"], mode)) >= 4.5, \
            "Watch sign-in action must use readable text on its accent surface"


def test_accent_text_retains_contrast_on_its_composited_selection_tint():
    css = (STATIC / "theme.css").read_text()
    tokens = {name: (light.lower(), dark.lower()) for name, light, dark in re.findall(
        r"(--[a-z-]+):\s*light-dark\((#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)", css)}
    tints = re.search(r"--accent-soft:\s*light-dark\(rgba\(([^)]+)\),\s*rgba\(([^)]+)\)\)", css)
    assert tints
    for mode in (0, 1):
        red, green, blue, alpha = map(float, tints[mode + 1].split(","))
        for background in ("--panel", "--panel-strong", "--overlay-surface", "--surface-hover"):
            base = tokens[background][mode]
            channels = [int(base[index:index + 2], 16) for index in (1, 3, 5)]
            # Evaluate the composed surface, not just the unpainted solid token.
            mixed = [channel * alpha + below * (1 - alpha)
                     for channel, below in zip((red, green, blue), channels)]
            def luminance(values):
                values = [value / 255 for value in values]
                return sum((value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4) * weight
                           for value, weight in zip(values, (.2126, .7152, .0722)))
            lower, upper = sorted((_luminance(tokens["--accent"][mode]), luminance(mixed)))
            assert (upper + .05) / (lower + .05) >= 4.5, (mode, background)
