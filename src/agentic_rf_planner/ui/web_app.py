"""Optional dev server wrapper for web app."""

# This file can be used to run the app during development
# For production, use: uvicorn agentic_rf_planner.api.rest:app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agentic_rf_planner.api.rest:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_keep_alive=300,  # 5 minutes keep-alive timeout for long-running requests
    )

