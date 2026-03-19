import win32serviceutil
import win32service
import win32event
import servicemanager
import biometric_client
import sys
import os

class BiometricService(win32serviceutil.ServiceFramework):

    _svc_name_ = "VakratmaBiometric"

    _svc_display_name_ = "Vakratma Biometric Authentication Service"

    _svc_description_ = "Continuous Face Authentication Service"


    def __init__(self,args):

        win32serviceutil.ServiceFramework.__init__(self,args)

        self.stop_event = win32event.CreateEvent(None,0,0,None)


    def SvcStop(self):

        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)

        win32event.SetEvent(self.stop_event)


    def SvcDoRun(self):

        servicemanager.LogInfoMsg("Vakratma Service Started")

        biometric_client.main()


if __name__ == '__main__':

    win32serviceutil.HandleCommandLine(BiometricService)