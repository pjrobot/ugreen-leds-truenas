# ugreen_leds.py and the daemon embedded in ugreen-leds.yml are the same code: the YAML is what
# TrueNAS users paste, the .py is for everyone else. `make check` refuses to let them drift and
# byte-compiles the daemon. Needs python3 with PyYAML (apt install python3-yaml / pip install pyyaml).
.PHONY: check sync

check:
	@python3 -c 'import yaml; emb = yaml.safe_load(open("ugreen-leds.yml"))["configs"]["ugreen_leds_py"]["content"]; py = open("ugreen_leds.py").read(); assert emb == py, "ugreen_leds.py differs from the daemon embedded in ugreen-leds.yml (run: make sync)"; compile(py, "ugreen_leds.py", "exec"); print("ok: embedded daemon == ugreen_leds.py, and it compiles")'

# Re-embed ugreen_leds.py into ugreen-leds.yml after editing the .py (the .py is where edits go).
sync:
	@python3 -c 'py = open("ugreen_leds.py").read(); yml = open("ugreen-leds.yml").read(); head = yml.split("    content: |\n")[0]; body = "".join(("      " + l) if l.strip() else "\n" for l in py.splitlines(True)); open("ugreen-leds.yml", "w").write(head + "    content: |\n" + body); print("ugreen-leds.yml re-embedded from ugreen_leds.py")'
