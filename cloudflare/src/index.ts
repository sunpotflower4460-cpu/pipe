export default {
  async fetch(): Promise<Response> {
    return new Response('Code Memo Cloudflare shell', {
      headers: { 'content-type': 'text/plain; charset=utf-8' },
    });
  },
};
