.PHONY: check install

check:
	python3 -B -m unittest discover -s tests -v
	@awk 'END { printf "bin/launch: %d / 250 lines\n", NR; exit (NR > 250 || NR == 0) }' bin/launch
	@awk 'END { printf "bin/launch-schedule: %d / 150 lines\n", NR; exit (NR > 150 || NR == 0) }' bin/launch-schedule
	@find . -path './tests' -prune -o -path './.git' -prune -o -type f \( -name '*.py' -o -path './bin/*' \) -print0 | xargs -0 -r cat | awk 'END { printf "Production code: %d / 400 lines\n", NR; exit (NR > 400 || NR == 0) }'


PREFIX ?= $(HOME)/.local
CONFIG_DIR ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)

install:
	install -d "$(DESTDIR)$(PREFIX)/bin" "$(DESTDIR)$(CONFIG_DIR)/systemd/user"
	@set -eu; \
	instance="$(DESTDIR)$(CONFIG_DIR)/launch/instance.env"; \
	if [ -n "$(INSTANCE_CONFIG)" ] && [ ! -e "$$instance" ] && [ ! -L "$$instance" ]; then \
		install -d -m 700 "$$(dirname "$$instance")"; \
		tmp="$$(mktemp "$$instance.XXXXXX")"; \
		trap 'rm -f -- "$$tmp"' EXIT; \
		install -m 600 "$(INSTANCE_CONFIG)" "$$tmp"; \
		if ! ln -T -- "$$tmp" "$$instance"; then [ -e "$$instance" ] || [ -L "$$instance" ]; fi; \
	fi
	install -m 755 bin/launch bin/launch-schedule "$(DESTDIR)$(PREFIX)/bin/"
	install -m 644 systemd/launch-schedule.service systemd/launch-schedule.timer "$(DESTDIR)$(CONFIG_DIR)/systemd/user/"
