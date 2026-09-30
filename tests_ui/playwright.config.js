// Dashboard tests run the real page against stubbed Firebase and /owner APIs.
export default {
  testDir: ".",
  timeout: 20000,
  reporter: "list",
  use: {headless: true},
};
