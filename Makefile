CC = bash scripts/zig-cc.sh
SANITIZER_CC ?= clang
PYTHON ?= python3
VERSION ?= dev
MODE ?= release
TARGET_OS ?= $(shell uname -s | tr A-Z a-z)
TARGET_ARCH ?= $(shell uname -m)
ifeq ($(MODE),debug)
BUILD ?= build/debug
CFLAGS ?= -O0 -g
else ifeq ($(MODE),release)
BUILD ?= build
CFLAGS ?= -Os
ifeq ($(TARGET_OS),linux)
STRIP_LDFLAGS = -s
endif
else
$(error Unsupported MODE: $(MODE) (expected release or debug))
endif
CPPFLAGS += -D_POSIX_C_SOURCE=200809L -D_XOPEN_SOURCE=700 -D_DEFAULT_SOURCE -D_DARWIN_C_SOURCE -DCJSON_NESTING_LIMIT=32 -DBGX_VERSION='"$(VERSION)"' -Isrc -Ivendor/cjson
CFLAGS += -std=c17 -Wall -Wextra -Wpedantic -Werror
LDLIBS += -lm
SOURCES = src/main.c src/common.c src/registry.c src/process.c src/process_$(TARGET_OS).c vendor/cjson/cJSON.c

.DEFAULT_GOAL := build
.PHONY: build debug test sanitize check release install uninstall clean FORCE

build: $(BUILD)/bgx

debug:
	$(MAKE) MODE=debug build

# The CLI is small; rebuild each invocation so compiler/target/version changes
# cannot silently reuse a binary built with different command-line settings.
$(BUILD)/bgx: $(SOURCES) src/bgx.h vendor/cjson/cJSON.h Makefile scripts/zig-cc.sh .zig-version FORCE
	mkdir -p $(BUILD)
	TARGET_OS=$(TARGET_OS) TARGET_ARCH=$(TARGET_ARCH) $(CC) $(CPPFLAGS) $(CFLAGS) $(SOURCES) $(LDFLAGS) $(STRIP_LDFLAGS) $(LDLIBS) -o $@
ifeq ($(MODE),release)
ifeq ($(TARGET_OS),darwin)
	strip -x $@
endif
endif

FORCE:

test: build
	$(MAKE) BUILD=$(BUILD)/fault TESTING=1 build
	BGX_BIN=$(abspath $(BUILD)/bgx) BGX_FAULT_BIN=$(abspath $(BUILD)/fault/bgx) $(PYTHON) -m unittest discover -s tests -v

ifeq ($(TESTING),1)
CPPFLAGS += -DBGX_TESTING -DBGX_LAUNCH_TIMEOUT_MS=500 -DBGX_CLEANUP_TIMEOUT_MS=300
SOURCES += tests/faults.c
endif

sanitize:
	$(MAKE) MODE=debug CC="$(SANITIZER_CC)" BUILD=build/sanitize TARGET_OS=$(shell uname -s | tr A-Z a-z) TARGET_ARCH=$(shell uname -m) CFLAGS='-O1 -g -std=c17 -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' test

check: test sanitize

release:
	bash scripts/package.sh "$(TAG)"

PREFIX ?= /usr/local
install: build
	install -d "$(DESTDIR)$(PREFIX)/bin"
	install -m 755 $(BUILD)/bgx "$(DESTDIR)$(PREFIX)/bin/bgx"

uninstall:
	rm -f -- "$(DESTDIR)$(PREFIX)/bin/bgx"

clean:
	rm -rf -- build dist
