<script lang="ts">
  const PATHS: Record<string, string> = {
    mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Zm-7 9a7 7 0 0 0 14 0M12 19v3",
    pulse: "M3 12h4l3-8 4 16 3-8h4",
    radar: "M12 12 19 5M21 12a9 9 0 1 1-9-9M17 12a5 5 0 1 1-5-5M12 12h.01",
    cube: "M12 2 3 7v10l9 5 9-5V7l-9-5Zm0 0v20M3 7l9 5 9-5",
    play: "M7 4v16l13-8L7 4Z",
    stop: "M6 6h12v12H6z",
    trash: "M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13",
    split: "M4 12h16M8 6v12M16 6v12",
    tag: "M3 12V4h8l10 10-8 8L3 12Zm5-4h.01",
    check: "M5 13l4 4L19 7",
    x: "M6 6l12 12M18 6 6 18",
    chevron: "M9 6l6 6-6 6",
    sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
    replay: "M4 12a8 8 0 1 0 3-6.2M4 4v5h5",
    plus: "M12 5v14M5 12h14",
    next: "M5 12h14M13 6l6 6-6 6",
    back: "M19 12H5M11 6l-6 6 6 6",
    type: "M5 7V5h14v2M12 5v14M9 19h6",
    sparkles: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3ZM19 16l.7 1.8 1.8.7-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7L19 16Z",
    download: "M12 4v11m-5-5 5 5 5-5M5 20h14",
    upload: "M12 15V4m-5 5 5-5 5 5M5 20h14",
    grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  };

  let { name, size = 18, stroke = 1.8 }: { name: keyof typeof PATHS | string; size?: number; stroke?: number } = $props();
</script>

<svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width={stroke} stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d={PATHS[name]} />
</svg>
