const { Octokit } = require("@octokit/rest");
const OWNER = "TWOJE-USERNAME";
const REPO  = "TWOJE-REPO";
const PATH  = "baza.json";
const TOKEN = process.env.GH_TOKEN;

exports.handler = async () => {
  const octo = new Octokit({ auth: TOKEN });
  const { data } = await octo.repos.getContent({ owner: OWNER, repo: REPO, path: PATH });
  const content = Buffer.from(data.content, "base64").toString();
  return { statusCode: 200, body: content };
};
