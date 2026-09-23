CC ?= gcc
CFLAGS ?= -Wall -Wextra -O2 -g
CPPFLAGS ?= -I../ckl_config
LDFLAGS ?=

TARGET := ckl_daemon
SRCS := ckl_daemon.c ../ckl_config/config_store.c

.PHONY: all clean install

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $(SRCS) $(LDFLAGS)

install: $(TARGET)
	install -d $(DESTDIR)/usr/local/bin
	install -m 0755 $(TARGET) $(DESTDIR)/usr/local/bin/$(TARGET)

clean:
	rm -f $(TARGET)
