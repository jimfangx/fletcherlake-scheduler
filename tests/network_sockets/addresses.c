/* Inetd SSH fixture only: preserve tailscaled's trusted PROXY-v1 peer metadata. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <stdlib.h>

static int translated(int fd, struct sockaddr *address, socklen_t *length,
                      const char *function, const char *ip_name, const char *port_name)
{
    int (*original)(int, struct sockaddr *, socklen_t *) = dlsym(RTLD_NEXT, function);
    int result = original(fd, address, length);
    const char *ip = getenv(ip_name), *port = getenv(port_name);
    if (!result && address->sa_family == AF_INET &&
        *length >= sizeof(struct sockaddr_in) && ip && port) {
        struct sockaddr_in *ipv4 = (void *)address;
        inet_pton(AF_INET, ip, &ipv4->sin_addr);
        ipv4->sin_port = htons(atoi(port));
    }
    return result;
}

int getpeername(int fd, struct sockaddr *address, socklen_t *length)
{
    return translated(fd, address, length, "getpeername",
                      "FL_TEST_PROXY_SOURCE", "FL_TEST_PROXY_SOURCE_PORT");
}

int getsockname(int fd, struct sockaddr *address, socklen_t *length)
{
    return translated(fd, address, length, "getsockname",
                      "FL_TEST_PROXY_DESTINATION", "FL_TEST_PROXY_DESTINATION_PORT");
}
