/* Linux fixture only: send native SSH/BBCP tailnet connections through tailscaled. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static int handshake(int fd, const char *host, unsigned port)
{
    char request[256], response[4096];
    int size = snprintf(request, sizeof(request),
        "CONNECT %s:%u HTTP/1.1\r\nHost: %s:%u\r\n\r\n", host, port, host, port);
    for (int sent = 0; sent < size;) {
        ssize_t count = send(fd, request + sent, size - sent, MSG_NOSIGNAL);
        if (count <= 0) return -1;
        sent += count;
    }
    /* Read exactly the header: a pending SSH banner must remain in the socket. */
    for (size_t count = 0; count < sizeof(response) - 1; count++) {
        if (recv(fd, response + count, 1, 0) != 1) {
            dprintf(STDERR_FILENO, "Fixture CONNECT stopped after %zu header bytes\n", count);
            return -1;
        }
        response[count + 1] = '\0';
        if (count >= 3 && !strcmp(response + count - 3, "\r\n\r\n")) {
            if (!strncmp(response, "HTTP/1.1 200 ", 13) ||
                !strncmp(response, "HTTP/1.0 200 ", 13)) return 0;
            errno = EACCES;
            return -1;
        }
    }
    errno = EPROTO;
    return -1;
}

int connect(int fd, const struct sockaddr *address, socklen_t length)
{
    int (*original)(int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "connect");
    const char *proxy_port = getenv("FL_TEST_CONNECT_PROXY_PORT");
    struct in_addr target;
    unsigned port;
    if (!proxy_port) return original(fd, address, length);
    if (address->sa_family == AF_INET) {
        const struct sockaddr_in *ipv4 = (const void *)address;
        target = ipv4->sin_addr;
        port = ntohs(ipv4->sin_port);
    } else if (address->sa_family == AF_INET6) {
        const struct sockaddr_in6 *ipv6 = (const void *)address;
        if (!IN6_IS_ADDR_V4MAPPED(&ipv6->sin6_addr)) return original(fd, address, length);
        memcpy(&target, ipv6->sin6_addr.s6_addr + 12, sizeof(target));
        port = ntohs(ipv6->sin6_port);
    } else return original(fd, address, length);
    if ((ntohl(target.s_addr) & 0xffc00000U) != 0x64400000U)
        return original(fd, address, length);

    struct sockaddr_storage proxy = {0};
    socklen_t proxy_length;
    if (address->sa_family == AF_INET) {
        struct sockaddr_in *ipv4 = (void *)&proxy;
        ipv4->sin_family = AF_INET;
        ipv4->sin_port = htons(atoi(proxy_port));
        inet_pton(AF_INET, "127.0.0.1", &ipv4->sin_addr);
        proxy_length = sizeof(*ipv4);
    } else {
        struct sockaddr_in6 *ipv6 = (void *)&proxy;
        ipv6->sin6_family = AF_INET6;
        ipv6->sin6_port = htons(atoi(proxy_port));
        inet_pton(AF_INET6, "::ffff:127.0.0.1", &ipv6->sin6_addr);
        proxy_length = sizeof(*ipv6);
    }
    int flags = fcntl(fd, F_GETFL);
    struct timeval deadline = {.tv_sec = 5}, previous;
    socklen_t previous_length = sizeof(previous);
    getsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &previous, &previous_length);
    fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &deadline, sizeof(deadline));
    int result = original(fd, (const void *)&proxy, proxy_length);
    char host[INET_ADDRSTRLEN];
    inet_ntop(AF_INET, &target, host, sizeof(host));
    if (!result) result = handshake(fd, host, port);
    int saved_errno = errno;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &previous, sizeof(previous));
    fcntl(fd, F_SETFL, flags);
    const char *trace = getenv("FL_TEST_CONNECT_TRACE");
    if (!result && trace) {
        int output = open(trace, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (output >= 0) {
            dprintf(output, "%s %u\n", host, port);
            close(output);
        }
    }
    errno = saved_errno;
    return result;
}
