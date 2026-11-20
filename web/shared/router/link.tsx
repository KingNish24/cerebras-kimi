"use client";

import React from "react";
import { Link as TanStackLink } from "@tanstack/react-router";

export interface UrlObject {
  pathname?: string | null;
  query?: Record<string, string | number | boolean | null | undefined>;
  hash?: string | null;
}

export type LinkHref = string | UrlObject;

export interface LinkProps
  extends Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "href"> {
  href: LinkHref;
  replace?: boolean;
  prefetch?: boolean | null;
  scroll?: boolean;
  shallow?: boolean;
  passHref?: boolean;
  legacyBehavior?: boolean;
}

function resolveHref(href: LinkHref): string {
  if (typeof href === "string") return href;
  const pathname = href.pathname ?? "";
  const params = new URLSearchParams();
  if (href.query) {
    for (const [key, value] of Object.entries(href.query)) {
      if (value !== undefined && value !== null) {
        params.set(key, String(value));
      }
    }
  }
  const search = params.toString();
  const hash = href.hash
    ? href.hash.startsWith("#")
      ? href.hash
      : `#${href.hash}`
    : "";
  return `${pathname}${search ? `?${search}` : ""}${hash}`;
}

function isExternalHref(href: string): boolean {
  return (
    href.startsWith("http://") ||
    href.startsWith("https://") ||
    href.startsWith("//") ||
    href.startsWith("mailto:") ||
    href.startsWith("tel:")
  );
}

function parseInternalHref(href: string): {
  to: string;
  search?: Record<string, string>;
  hash?: string;
} {
  try {
    const url = new URL(href, "http://localhost");
    const searchObj = Object.fromEntries(url.searchParams.entries());
    return {
      to: url.pathname,
      search: Object.keys(searchObj).length > 0 ? searchObj : undefined,
      hash: url.hash ? url.hash.slice(1) : undefined,
    };
  } catch {
    return { to: href };
  }
}

export const Link = React.forwardRef<HTMLAnchorElement, LinkProps>(
  function Link(
    {
      href,
      replace,
      prefetch,
      scroll,
      shallow: _shallow,
      passHref: _passHref,
      legacyBehavior: _legacyBehavior,
      target,
      download,
      children,
      ...rest
    },
    ref,
  ) {
    const resolved = resolveHref(href);

    if (
      isExternalHref(resolved) ||
      target === "_blank" ||
      download !== undefined
    ) {
      return (
        <a
          ref={ref}
          href={resolved}
          target={target}
          download={download}
          {...rest}
        >
          {children}
        </a>
      );
    }

    const { to, search, hash } = parseInternalHref(resolved);

    return (
      <TanStackLink
        ref={ref}
        to={to as never}
        search={search as never}
        hash={hash}
        replace={replace}
        resetScroll={scroll}
        preload={prefetch === false ? false : "intent"}
        target={target}
        {...rest}
      >
        {children}
      </TanStackLink>
    );
  },
);

export default Link;
