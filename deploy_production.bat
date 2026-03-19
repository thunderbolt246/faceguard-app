@echo off
REM ============================================================================
REM PRODUCTION DEPLOYMENT SCRIPT (Windows)
REM Uses gunicorn (WSGI server) instead of Flask dev server
REM ============================================================================

echo.
echo ========================================================================
echo   BIOMETRIC SYSTEM - PRODUCTION DEPLOYMENT
echo ========================================================================
echo.

REM Check if DB_PASSWORD is set
if "%DB_PASSWORD%"=="" (
    echo [ERROR] DB_PASSWORD environment variable not set!
    echo.
    echo Set it with: set DB_PASSWORD=your_password
    echo.
    pause
    exit /b 1
)

echo [OK] DB_PASSWORD is set
echo.

REM Check if PostgreSQL is running
pg_isready -h localhost -p 5432 >nul 2>&1
if errorlevel 1 (
    echo [WARNING] PostgreSQL may not be running on localhost:5432
    echo Please verify PostgreSQL is installed and running
    echo.
)

REM Check if gunicorn is installed
where gunicorn >nul 2>&1
if errorlevel 1 (
    echo Installing gunicorn...
    pip install gunicorn
    if errorlevel 1 (
        echo [ERROR] Failed to install gunicorn
        pause
        exit /b 1
    )
)

echo [OK] Gunicorn is installed
echo.

echo ========================================================================
echo   PRODUCTION CONFIGURATION
echo ========================================================================
echo.
echo Workers:      4 (multi-process)
echo Worker class: gthread (thread-based)
echo Threads:      2 per worker
echo Bind:         0.0.0.0:5000
echo Timeout:      30s
echo App:          app_postgresql:app
echo.

echo ========================================================================
echo   STARTING PRODUCTION SERVER
echo ========================================================================
echo.
echo Press Ctrl+C to stop the server
echo.

REM Start gunicorn with production settings
gunicorn ^
    --workers 4 ^
    --worker-class gthread ^
    --threads 2 ^
    --bind 0.0.0.0:5000 ^
    --timeout 30 ^
    --access-logfile - ^
    --error-logfile - ^
    --log-level info ^
    --capture-output ^
    app_postgresql:app

pause