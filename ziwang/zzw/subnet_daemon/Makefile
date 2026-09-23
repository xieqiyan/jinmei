CC ?= gcc
CFLAGS ?= -Wall -Wextra -O2 -g
LDFLAGS ?=

TARGET := zzw_daemon
SRCS := subnet_daemon.c

.PHONY: all clean install

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CC) $(CFLAGS) -o $@ $(SRCS) $(LDFLAGS)

install: $(TARGET)
	install -d $(DESTDIR)/usr/local/bin
	install -m 0755 $(TARGET) $(DESTDIR)/usr/local/bin/$(TARGET)

clean:
	rm -f $(TARGET)
