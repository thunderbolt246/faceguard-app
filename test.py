import cv2

cap = cv2.VideoCapture(0)

ret, frame = cap.read()

print("Camera Open:", cap.isOpened())
print("Frame Read:", ret)

cap.release()

