from src.handlers.efs import EFSBackupHandler
from src.handlers.s3 import S3BackupHandler
from src.handlers.ebs import EBSBackupHandler
from src.handlers.rds import RDSBackupHandler

HANDLERS = {
    "efs": EFSBackupHandler,
    "s3": S3BackupHandler,
    "ebs": EBSBackupHandler,
    "rds": RDSBackupHandler,
}
