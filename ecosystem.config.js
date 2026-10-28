module.exports = {
  apps: [
    {
      name: "Petal-AI-Backend",
      exec_mode: "fork",
      instances: 1,
      script: "source env/bin/activate && uvicorn app.main:app --port 8000",
      env_production: {
        NODE_ENV: "production",
        PORT: 8000,
        
      },
      env_staging: {
        NODE_ENV: "staging",
        PORT: 8000,
      },
      env_development: {
        NODE_ENV: "development",
        PORT: 8000,
      },
    },
  ],
};
